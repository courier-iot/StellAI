package com.stellai.app

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Switch
import androidx.compose.ui.Alignment
import kotlinx.coroutines.delay
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

/**
 * Settings → Stella → Personality: describe Stella in your own words. Kept on the server, so every phone, the desktop
 * chat and the avatar screen use it; empty means her default character.
 */
@Composable
fun PersonalitySettings() {
    val s = App.instance.settings
    val scope = rememberCoroutineScope()
    var text by remember { mutableStateOf("") }
    var saved by remember { mutableStateOf("") }
    var hint by remember { mutableStateOf("") }
    var max by remember { mutableStateOf(600) }
    var status by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(Unit) {
        try {
            val p = withContext(Dispatchers.IO) { HubApi.personality(s.serverUrl, s.apiKey) }
            text = p.optString("text"); saved = text
            hint = p.optString("default"); max = p.optInt("max", 600)
        } catch (_: Exception) {
            status = "✗ Couldn't reach the server: the personality is kept there"
        }
    }
    Text("Personality", style = MaterialTheme.typography.labelLarge)
    BrainHint("Describe ${s.assistantName.ifBlank { "Stella" }} in your own words: her character, tone and manner. " +
        "It replaces her default character on every device. Leave it empty for the default.")
    OutlinedTextField(
        value = text,
        onValueChange = { text = it.take(max) },
        placeholder = { if (hint.isNotBlank()) Text(hint) },
        supportingText = { Text("${text.length} / $max") },
        minLines = 3,
        modifier = Modifier.fillMaxWidth(),
    )
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(enabled = text.trim() != saved, onClick = {
            scope.launch {
                status = try {
                    val r = withContext(Dispatchers.IO) { HubApi.savePersonality(s.serverUrl, s.apiKey, text.trim()) }
                    saved = r.optString("text"); text = saved
                    App.instance.modes.refresh()
                    "✓ Saved for every device"
                } catch (e: Exception) {
                    "✗ " + friendlyErrorText(e, s.serverUrl)
                }
            }
        }) { Text("Save personality") }
        if (text.isNotBlank()) TextButton(onClick = { text = "" }) { Text("Use the default") }
    }
    status?.let { StatusLine(it, okColor = Space.Accent, badColor = Space.Red) }
}

/**
 * Settings → Skills: step-by-step guides Stella reads before a kind of task (a FreeCAD part, a KiCad board, fixing a
 * build). Built-in ones can be replaced by your own of the same name; yours can be changed or deleted.
 */
@Composable
fun SkillsSettings() {
    val s = App.instance.settings
    val scope = rememberCoroutineScope()
    var skills by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    var status by remember { mutableStateOf<String?>(null) }
    var editing by remember { mutableStateOf<JSONObject?>(null) }
    fun show(a: JSONArray) { skills = (0 until a.length()).map { a.getJSONObject(it) } }
    LaunchedEffect(Unit) {
        try {
            show(withContext(Dispatchers.IO) { HubApi.skills(s.serverUrl, s.apiKey) })
        } catch (e: Exception) {
            status = "✗ " + friendlyErrorText(e, s.serverUrl)
        }
    }
    BrainHint("Short how-to guides ${s.assistantName.ifBlank { "Stella" }} reads before that kind of task, so a small " +
        "model follows steps that work. Add your own, or ask her to \"save how we did this as a skill\".")
    skills.forEach { sk ->
        Column(Modifier.fillMaxWidth()) {
            Text(sk.optString("name") + when (sk.optString("source")) {
                "built-in" -> "  (built-in)"
                "stella" -> "  (saved by ${s.assistantName.ifBlank { "Stella" }})"
                else -> ""
            }, style = MaterialTheme.typography.bodyMedium)
            BrainHint(sk.optString("description"))
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                TextButton(onClick = { editing = JSONObject(sk.toString()) }) {
                    Text(if (sk.optString("source") == "built-in") "View / replace" else "Edit")
                }
                if (sk.optString("source") != "built-in") TextButton(onClick = {
                    scope.launch {
                        status = try {
                            show(withContext(Dispatchers.IO) { HubApi.deleteSkill(s.serverUrl, s.apiKey, sk.optString("name")) })
                            "✓ Deleted ${sk.optString("name")}"
                        } catch (e: Exception) {
                            "✗ " + friendlyErrorText(e, s.serverUrl)
                        }
                    }
                }) { Text(if (sk.optBoolean("replaces_built_in")) "Back to built-in" else "Delete") }
            }
        }
    }
    OutlinedButton(onClick = { editing = JSONObject() }) { Text("Add a skill") }
    status?.let { StatusLine(it, okColor = Space.Accent, badColor = Space.Red) }
    LessonsList()
    editing?.let { sk ->
        SkillDialog(sk, onDismiss = { editing = null }) { body ->
            scope.launch {
                status = try {
                    show(withContext(Dispatchers.IO) { HubApi.saveSkill(s.serverUrl, s.apiKey, body) })
                    editing = null
                    "✓ Saved ${body.optString("name")}"
                } catch (e: Exception) {
                    "✗ " + friendlyErrorText(e, s.serverUrl)
                }
            }
        }
    }
}

@Composable
private fun SkillDialog(skill: JSONObject, onDismiss: () -> Unit, onSave: (JSONObject) -> Unit) {
    val isNew = !skill.has("name")
    var name by remember { mutableStateOf(skill.optString("name")) }
    var desc by remember { mutableStateOf(skill.optString("description")) }
    var triggers by remember { mutableStateOf(if (skill.optString("source") == "built-in") "" else skill.optString("triggers")) }
    var body by remember { mutableStateOf(skill.optString("body")) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(if (isNew) "New skill" else if (skill.optString("source") == "built-in") "Replace ${skill.optString("name")}" else "Edit ${skill.optString("name")}") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(name, { name = it.lowercase().replace(' ', '-').take(40) }, label = { Text("Name") },
                    placeholder = { Text("e.g. solder-smd") }, singleLine = true, enabled = isNew, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(desc, { desc = it.take(160) }, label = { Text("When to use it (one line)") },
                    modifier = Modifier.fillMaxWidth())
                OutlinedTextField(triggers, { triggers = it.take(200) }, label = { Text("Words that suggest it (optional)") },
                    placeholder = { Text("solder, smd, reflow") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(body, { body = it.take(6000) }, label = { Text("Steps") }, minLines = 6,
                    modifier = Modifier.fillMaxWidth().heightIn(max = 360.dp).padding(bottom = 4.dp))
            }
        },
        confirmButton = {
            TextButton(enabled = name.length >= 2 && desc.isNotBlank() && body.isNotBlank(), onClick = {
                onSave(JSONObject().put("name", name).put("description", desc).put("triggers", triggers).put("body", body))
            }) { Text("Save") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun BrainHint(text: String) {
    Text(text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
}


/** Lessons she wrote from your corrections and thumbs-downs; each can be forgotten. */
@Composable
private fun LessonsList() {
    val s = App.instance.settings
    val scope = rememberCoroutineScope()
    var items by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    fun show(a: JSONArray) { items = (0 until a.length()).map { a.getJSONObject(it) }.reversed() }
    LaunchedEffect(Unit) { try { show(withContext(Dispatchers.IO) { HubApi.lessons(s.serverUrl, s.apiKey) }) } catch (_: Exception) {} }
    Text("Lessons from your corrections", style = MaterialTheme.typography.labelLarge)
    if (items.isEmpty()) BrainHint("None yet. When you correct her (\"no, that's wrong…\") or give a reply a thumbs-down " +
        "with a note (long-press the reply), she writes a one-line lesson here.")
    items.forEach { l ->
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(l.optString("text"), style = MaterialTheme.typography.bodyMedium)
                BrainHint(if (l.optString("skill") == "general") "Every chat" else "With the ${l.optString("skill")} skill")
            }
            TextButton(onClick = {
                scope.launch {
                    try { show(withContext(Dispatchers.IO) { HubApi.deleteLesson(s.serverUrl, s.apiKey, l.optString("id")) }) } catch (_: Exception) {}
                }
            }) { Text("Forget") }
        }
    }
}

/** Settings → Bigger brain: a mixture-of-experts model in the computer's memory for design work and hard problems. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun DeepSettings() {
    val s = App.instance.settings
    val scope = rememberCoroutineScope()
    var st by remember { mutableStateOf<JSONObject?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var confirm by remember { mutableStateOf(false) }
    suspend fun load() {
        try { st = withContext(Dispatchers.IO) { HubApi.deep(s.serverUrl, s.apiKey) }; error = null }
        catch (e: Exception) { error = "✗ " + friendlyErrorText(e, s.serverUrl) }
    }
    val finding = st?.optJSONObject("finding")?.optString("state") == "running"
    LaunchedEffect(finding) {
        load()
        while (finding) { delay(3000); load(); if (st?.optJSONObject("finding")?.optString("state") != "running") break }
    }
    val name = s.assistantName.ifBlank { "Stella" }
    BrainHint("A bigger model that runs from the computer's memory instead of the graphics card: slower to answer, " +
        "but it reasons better. $name uses it for design work, multi-step plans, anything the quick model fails at " +
        "twice, or when you ask her to \"think harder\". It loads when needed and leaves memory after a quiet spell.")
    error?.let { StatusLine(it, badColor = Space.Red) }
    val d = st ?: return
    Text(if (d.optBoolean("available")) "${d.optString("label").ifBlank { d.optString("url") }}" +
        (if (d.optDouble("tok_s", 0.0) > 0) " · ${d.optDouble("tok_s")} tokens/s" else "") +
        (if (d.optBoolean("running")) " · loaded now" else " · loads when needed") else "Not set up yet.",
        style = MaterialTheme.typography.bodyMedium)
    if (!d.isNull("free_ram_gb")) BrainHint("Free memory right now: ${d.optDouble("free_ram_gb")} GB")
    d.optJSONObject("finding")?.let { f ->
        val pct = if (f.optLong("total") > 0) " (${100 * f.optLong("done") / f.optLong("total")}%)" else ""
        StatusLine((if (f.optString("state") == "failed") "✗ " else if (f.optString("state") == "done") "✓ " else "") +
            f.optString("text") + if (finding) pct else "", okColor = Space.Accent, badColor = Space.Red)
    }
    Button(enabled = !finding && d.optBoolean("exe_found"), onClick = { confirm = true }) {
        Text(if (d.optBoolean("available")) "Find a better fit again" else "Find one that fits")
    }
    if (!d.optBoolean("exe_found")) BrainHint("This server can't start a second model by itself (set one up on another computer in the desktop chat's settings).")
    Text("When to use it", style = MaterialTheme.typography.labelLarge)
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        listOf("auto" to "Auto", "design" to "Design work", "always" to "Always", "off" to "Off").forEach { (v, label) ->
            FilterChip(selected = d.optString("lane") == v, label = { Text(label) }, onClick = {
                scope.launch {
                    try { st = withContext(Dispatchers.IO) { HubApi.saveDeep(s.serverUrl, s.apiKey, JSONObject().put("lane", v)) } }
                    catch (e: Exception) { error = "✗ " + friendlyErrorText(e, s.serverUrl) }
                }
            })
        }
    }
    BrainHint("Auto: design work, plans and hard problems. Design work: only CAD, Blender and PCB.")
    if (confirm) AlertDialog(
        onDismissRequest = { confirm = false },
        title = { Text("Find a bigger model?") },
        text = { Text("This checks the free memory, downloads the best model that fits (often 10–15 GB) and loads it " +
            "to measure it. It can take a while; $name keeps working meanwhile.") },
        confirmButton = { TextButton(onClick = {
            confirm = false
            scope.launch {
                try { withContext(Dispatchers.IO) { HubApi.findDeep(s.serverUrl, s.apiKey) }; load() }
                catch (e: Exception) { error = "✗ " + friendlyErrorText(e, s.serverUrl) }
            }
        }) { Text("Go ahead") } },
        dismissButton = { TextButton(onClick = { confirm = false }) { Text("Cancel") } },
    )
}

/** Settings → Test set: the rated and corrected replies, replayed with a score; plus the long-answer check. */
@Composable
fun TestSetSettings() {
    val s = App.instance.settings
    val scope = rememberCoroutineScope()
    var data by remember { mutableStateOf<JSONObject?>(null) }
    var check by remember { mutableStateOf(true) }
    var tick by remember { mutableStateOf(0) }
    LaunchedEffect(tick) {
        try {
            data = withContext(Dispatchers.IO) { HubApi.evals(s.serverUrl, s.apiKey) }
            check = withContext(Dispatchers.IO) { HubApi.selfCheck(s.serverUrl, s.apiKey) }.optBoolean("enabled", true)
        } catch (_: Exception) {}
        if (data?.optJSONObject("running")?.optString("state") == "running") { delay(4000); tick++ }
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f).padding(end = 12.dp)) {
            Text("Check long answers")
            BrainHint("Before a long answer stands, it's checked once for missing parts, numbers that don't add up and code that can't run, and fixed in place. A few seconds longer.")
        }
        Switch(checked = check, onCheckedChange = { on ->
            check = on
            scope.launch { try { withContext(Dispatchers.IO) { HubApi.saveSelfCheck(s.serverUrl, s.apiKey, on) } } catch (_: Exception) {} }
        })
    }
    BrainHint("Long-press one of her replies for Good answer or Bad answer. Those replies become her test set: each " +
        "run asks the model the same requests again and a strict reviewer scores the answers, so you can see whether " +
        "a change made her better or worse. It also runs by itself at night after something changed.")
    val d = data ?: return
    val cases = d.optJSONArray("cases")?.length() ?: 0
    val runs = d.optJSONArray("runs")
    val last = runs?.optJSONObject(0)
    val prev = runs?.optJSONObject(1)
    Text("$cases case${if (cases == 1) "" else "s"}" + (last?.let { " · last run: ${it.optInt("passed")} of ${it.optInt("total")} passed" +
        (prev?.let { p -> " (before: ${p.optInt("passed")} of ${p.optInt("total")})" } ?: "") } ?: ""),
        style = MaterialTheme.typography.bodyMedium)
    val running = d.optJSONObject("running")?.takeIf { it.optString("state") == "running" }
    running?.let { BrainHint("Running… ${it.optInt("done")} of ${it.optInt("total")}") }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(enabled = running == null && cases > 0, onClick = {
            scope.launch { try { withContext(Dispatchers.IO) { HubApi.runEvals(s.serverUrl, s.apiKey, "fast") } } catch (_: Exception) {}; tick++ }
        }) { Text("Run now") }
        OutlinedButton(enabled = running == null && cases > 0, onClick = {
            scope.launch { try { withContext(Dispatchers.IO) { HubApi.runEvals(s.serverUrl, s.apiKey, "deep") } } catch (_: Exception) {}; tick++ }
        }) { Text("On the bigger model") }
    }
    last?.optJSONArray("details")?.let { det ->
        (0 until det.length()).map { det.getJSONObject(it) }.filter { !it.optBoolean("pass") }.take(5).forEach {
            StatusLine("✗ " + it.optString("request").take(80) + " — " + it.optString("why"), badColor = Space.Red)
        }
    }
}

/** Settings → Background jobs: things she does by herself at set times. */
@Composable
fun JobsSettings() {
    val s = App.instance.settings
    val scope = rememberCoroutineScope()
    var jobs by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    var status by remember { mutableStateOf<String?>(null) }
    fun show(a: JSONArray) { jobs = (0 until a.length()).map { a.getJSONObject(it) } }
    LaunchedEffect(Unit) { try { show(withContext(Dispatchers.IO) { HubApi.jobs(s.serverUrl, s.apiKey) }) } catch (e: Exception) { status = "✗ " + friendlyErrorText(e, s.serverUrl) } }
    val name = s.assistantName.ifBlank { "Stella" }
    BrainHint("Things $name does by herself at set times (the server's clock). Each run goes into the job's own chat " +
        "and a reminder rings when it's done. To add one, just ask her: \"every weekday at 7:30, summarize last " +
        "night's GitHub builds\".")
    if (jobs.isEmpty()) BrainHint("No jobs yet.")
    val every = mapOf("daily" to "every day", "weekdays" to "weekdays", "weekly" to "weekly", "hourly" to "every hour", "once" to "once")
    jobs.forEach { j ->
        Column(Modifier.fillMaxWidth()) {
            Text(j.optString("name") + " · " + (every[j.optString("repeat")] ?: j.optString("repeat")) +
                (if (j.optString("repeat") != "hourly") " " + j.optString("time") else "") +
                (if (j.optBoolean("running")) " · running now" else ""), style = MaterialTheme.typography.bodyMedium)
            BrainHint(j.optString("prompt").take(140))
            if (j.optString("last_result").isNotBlank()) BrainHint("Last: " + j.optString("last_result"))
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                TextButton(onClick = {
                    scope.launch {
                        status = try { withContext(Dispatchers.IO) { HubApi.runJob(s.serverUrl, s.apiKey, j.optString("id")) }
                            "✓ Started ${j.optString("name")}: it runs in its own chat" } catch (e: Exception) { "✗ " + friendlyErrorText(e, s.serverUrl) }
                    }
                }) { Text("Run now") }
                TextButton(onClick = {
                    scope.launch {
                        try { show(withContext(Dispatchers.IO) { HubApi.deleteJob(s.serverUrl, s.apiKey, j.optString("id")) }) }
                        catch (e: Exception) { status = "✗ " + friendlyErrorText(e, s.serverUrl) }
                    }
                }) { Text("Delete") }
            }
        }
    }
    status?.let { StatusLine(it, okColor = Space.Accent, badColor = Space.Red) }
}
