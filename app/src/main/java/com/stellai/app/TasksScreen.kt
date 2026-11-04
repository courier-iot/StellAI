package com.stellai.app

import android.Manifest
import android.app.DatePickerDialog
import android.app.TimePickerDialog
import android.content.pm.PackageManager
import android.os.Build
import android.text.format.DateFormat
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import kotlinx.coroutines.launch
import java.util.Calendar

/** Upcoming reminders and your lists (shopping, parts, to-do), shared by all devices. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TasksScreen(onDone: () -> Unit) {
    val app = App.instance
    val rm = app.reminders
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val name = app.settings.assistantName.ifBlank { "Stella" }

    var newText by rememberSaveable { mutableStateOf("") }
    var due by remember { mutableLongStateOf(defaultDue()) }
    var repeat by rememberSaveable { mutableStateOf("none") }
    var repeatMenu by remember { mutableStateOf(false) }
    var newList by rememberSaveable { mutableStateOf("") }
    var newListItem by rememberSaveable { mutableStateOf("") }
    val itemDrafts = remember { mutableStateOf(mapOf<String, String>()) }

    var notifAllowed by remember {
        mutableStateOf(Build.VERSION.SDK_INT < 33 ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED)
    }
    val askNotif = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { notifAllowed = it }

    LaunchedEffect(Unit) {
        rm.refresh()
        rm.refreshLists()
    }

    fun pickDateTime() {
        val c = Calendar.getInstance().apply { timeInMillis = due }
        DatePickerDialog(context, { _, y, m, d ->
            TimePickerDialog(context, { _, h, min ->
                due = Calendar.getInstance().apply { set(y, m, d, h, min, 0); set(Calendar.MILLISECOND, 0) }.timeInMillis
            }, c.get(Calendar.HOUR_OF_DAY), c.get(Calendar.MINUTE), DateFormat.is24HourFormat(context)).show()
        }, c.get(Calendar.YEAR), c.get(Calendar.MONTH), c.get(Calendar.DAY_OF_MONTH)).show()
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Reminders & lists") },
                navigationIcon = { IconButton(onClick = onDone) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") } },
                actions = {
                    IconButton(onClick = { scope.launch { rm.refresh(); rm.refreshLists() } }) {
                        Icon(Icons.Filled.Refresh, contentDescription = "Refresh", tint = Space.Accent)
                    }
                },
            )
        },
    ) { padding ->
        LazyColumn(
            Modifier.fillMaxSize().padding(padding).imePadding(),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            if (!notifAllowed) {
                item {
                    Column(Modifier.fillMaxWidth().border(1.dp, Space.Gold, RoundedCornerShape(12.dp)).padding(12.dp)) {
                        Text("Notifications are off, so reminders can't ring on this phone.", color = Space.Gold)
                        TextButton(onClick = { askNotif.launch(Manifest.permission.POST_NOTIFICATIONS) }) { Text("Allow notifications") }
                    }
                }
            }
            if (rm.status.isNotEmpty()) item { Text(rm.status, color = Space.Red, style = MaterialTheme.typography.bodySmall) }

            item { TasksHeader("Reminders") }
            item {
                Text("They ring on all your phones. Tip: just tell $name “remind me to … at 5”.",
                    style = MaterialTheme.typography.bodySmall, color = Space.Muted)
            }
            val upcoming = rm.items.filter { !it.done }
            if (upcoming.isEmpty()) item { Text("No upcoming reminders.", color = Space.Muted) }
            items(upcoming, key = { "r-" + it.id }) { r ->
                TaskRow {
                    Column(Modifier.weight(1f).padding(vertical = 6.dp)) {
                        Text(r.text)
                        Text(
                            DateFormat.format("EEE, MMM d · " + if (DateFormat.is24HourFormat(context)) "HH:mm" else "h:mm a", r.dueAt).toString() +
                                if (r.repeat != "none") " · ${r.repeat}" else "",
                            style = HudLabel.copy(fontSize = 10.sp, letterSpacing = 0.5.sp), color = Space.Muted,
                        )
                    }
                    IconButton(onClick = { scope.launch { rm.cancel(r.id) } }) {
                        Icon(Icons.Filled.Delete, contentDescription = "Cancel reminder", tint = Space.Muted)
                    }
                }
            }
            item {
                OutlinedTextField(value = newText, onValueChange = { newText = it.take(300) }, singleLine = true,
                    placeholder = { Text("New reminder") }, modifier = Modifier.fillMaxWidth())
            }
            item {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { pickDateTime() }) {
                        Text(DateFormat.format("EEE MMM d, " + if (DateFormat.is24HourFormat(context)) "HH:mm" else "h:mm a", due).toString())
                    }
                    Box {
                        OutlinedButton(onClick = { repeatMenu = true }) { Text(if (repeat == "none") "Once" else repeat.replaceFirstChar { it.uppercase() }) }
                        DropdownMenu(expanded = repeatMenu, onDismissRequest = { repeatMenu = false }) {
                            listOf("none" to "Once", "daily" to "Daily", "weekdays" to "Weekdays", "weekly" to "Weekly", "monthly" to "Monthly")
                                .forEach { (v, l) -> DropdownMenuItem(text = { Text(l) }, onClick = { repeat = v; repeatMenu = false }) }
                        }
                    }
                    Spacer(Modifier.weight(1f))
                    Button(enabled = newText.isNotBlank(), onClick = {
                        val t = newText
                        newText = ""
                        scope.launch { rm.add(t, due, repeat) }
                    }) { Text("Add") }
                }
            }

            item { TasksHeader("Lists") }
            if (rm.lists.isEmpty()) item {
                Text("No lists yet. Say “add flux and solder wick to my parts list”, or start one below.", color = Space.Muted,
                    style = MaterialTheme.typography.bodySmall)
            }
            rm.lists.forEach { (list, entries) ->
                item(key = "l-$list") {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(list.replaceFirstChar { it.uppercase() }, color = Space.Text, modifier = Modifier.weight(1f).padding(top = 8.dp))
                        if (entries.any { it.done }) TextButton(onClick = { scope.launch { rm.clearTicked(list) } }) { Text("Clear ticked") }
                    }
                }
                items(entries, key = { "i-" + it.id }) { it2 ->
                    TaskRow {
                        Checkbox(checked = it2.done, onCheckedChange = { c -> scope.launch { rm.setItem(it2.id, c) } })
                        Text(it2.text, modifier = Modifier.weight(1f),
                            color = if (it2.done) Space.Muted else Space.Text,
                            textDecoration = if (it2.done) TextDecoration.LineThrough else null)
                        IconButton(onClick = { scope.launch { rm.deleteItem(it2.id) } }) {
                            Icon(Icons.Filled.Delete, contentDescription = "Delete", tint = Space.Muted)
                        }
                    }
                }
                item(key = "add-$list") {
                    val draft = itemDrafts.value[list].orEmpty()
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        OutlinedTextField(value = draft, onValueChange = { itemDrafts.value = itemDrafts.value + (list to it.take(200)) },
                            singleLine = true, placeholder = { Text("Add to $list") }, modifier = Modifier.weight(1f))
                        TextButton(enabled = draft.isNotBlank(), onClick = {
                            itemDrafts.value = itemDrafts.value - list
                            scope.launch { rm.addItem(list, draft) }
                        }) { Text("Add") }
                    }
                }
            }
            item {
                Row(Modifier.padding(top = 12.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(value = newList, onValueChange = { newList = it.take(40) }, singleLine = true,
                        placeholder = { Text("New list") }, modifier = Modifier.weight(1f))
                    OutlinedTextField(value = newListItem, onValueChange = { newListItem = it.take(200) }, singleLine = true,
                        placeholder = { Text("First item") }, modifier = Modifier.weight(1f))
                    TextButton(enabled = newList.isNotBlank() && newListItem.isNotBlank(), onClick = {
                        val l = newList
                        val i = newListItem
                        newList = ""
                        newListItem = ""
                        scope.launch { rm.addItem(l, i) }
                    }) { Text("Add") }
                }
            }
        }
    }
}

private fun defaultDue(): Long = Calendar.getInstance().apply {
    add(Calendar.HOUR_OF_DAY, 1)
    set(Calendar.MINUTE, 0)
    set(Calendar.SECOND, 0)
    set(Calendar.MILLISECOND, 0)
}.timeInMillis

@Composable
private fun TasksHeader(text: String) {
    Text(text.uppercase(), style = HudLabel.copy(fontSize = 12.sp, letterSpacing = 2.sp), color = Space.Accent,
        modifier = Modifier.padding(top = 10.dp))
}

@Composable
private fun TaskRow(content: @Composable androidx.compose.foundation.layout.RowScope.() -> Unit) {
    Row(
        Modifier.fillMaxWidth().border(1.dp, Space.Line, RoundedCornerShape(12.dp)).padding(start = 8.dp, end = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
        content = content,
    )
}
