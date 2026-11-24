package com.stellai.app

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * The model the on-phone brain suggests: the first time the app opens (a model that suits this phone), or a better
 * one it found later. Shown over any screen.
 */
@Composable
fun OnPhoneOfferDialog() {
    val brain = App.instance.onPhone
    val offer = brain.offer ?: return
    val name = ModelPicker.displayName(offer.candidate.repo)
    val size = ModelPicker.sizeText(offer.file.size)
    val mobile = brain.onMobileData && !App.instance.settings.onPhoneMobileData
    val stella = App.instance.settings.assistantName.ifBlank { "Stella" }
    val speech = App.instance.speech
    val offerSpeech = !brain.offerIsUpgrade && speech.hasProgram && speech.installed == null && speech.download == null
    var withSpeech by remember(offer) { mutableStateOf(true) }
    AlertDialog(
        onDismissRequest = { brain.dismissOffer(forever = false) },
        title = { Text(if (brain.offerIsUpgrade) "A better on-phone brain" else "Run $stella on this phone?") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (brain.offerIsUpgrade) {
                    Text("$name ($size) looks better than ${brain.installed?.name ?: "the one you have"} for this phone. " +
                        "Download it? It replaces the current one once it's ready.")
                } else {
                    Text("This phone can run my offline brain: $name ($size). With it I can still answer when the " +
                        "server can't be reached, or without a server at all.")
                }
                Text("${ModelPicker.sizeText(offer.file.size)} download · about ${"%.1f".format(offer.params)}B parameters · " +
                    ModelPicker.quantOf(offer.file.path).ifEmpty { "GGUF" },
                    style = MaterialTheme.typography.bodySmall, color = Space.Muted)
                if (offerSpeech) {
                    val sp = speech.recommended()
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(checked = withSpeech, onCheckedChange = { withSpeech = it })
                        Text("Also understand speech offline (${ModelPicker.sizeText(sp.bytes)} more)",
                            style = MaterialTheme.typography.bodySmall)
                    }
                }
                if (mobile) {
                    Text("You're on mobile data. Download now anyway ($size), or wait for Wi-Fi?",
                        style = MaterialTheme.typography.bodySmall, color = Space.Red)
                }
            }
        },
        confirmButton = {
            Column(horizontalAlignment = Alignment.End) {
                Button(onClick = {
                    brain.acceptOffer(confirmedMobile = mobile)
                    if (offerSpeech && withSpeech) speech.startDownload(confirmedMobile = mobile)
                }) {
                    Text(if (mobile) "Download on mobile data" else "Download")
                }
                if (mobile) TextButton(onClick = {
                    brain.acceptOffer(confirmedMobile = false)
                    if (offerSpeech && withSpeech) speech.startDownload(confirmedMobile = false)
                }) { Text("Wait for Wi-Fi") }
            }
        },
        dismissButton = {
            Column(horizontalAlignment = Alignment.End) {
                TextButton(onClick = { brain.dismissOffer(forever = false) }) { Text("Not now") }
                if (brain.offerIsUpgrade) TextButton(onClick = { brain.dismissOffer(forever = true) }) { Text("Don't suggest this one") }
            }
        },
    )
}

/** Settings → On-phone brain. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun OnPhoneSettings() {
    val app = App.instance
    val s = app.settings
    val brain = app.onPhone
    val scope = rememberCoroutineScope()
    val stella = s.assistantName.ifBlank { "Stella" }
    var link by remember { mutableStateOf("") }
    var linkStatus by remember { mutableStateOf("") }
    var confirmRemove by remember { mutableStateOf(false) }
    var copied by remember { mutableStateOf(false) }
    val pickFile = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) brain.importFile(uri)
    }

    // Who answers
    Text("Who answers")
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        listOf("server" to "Server", "auto" to "Auto", "phone" to "This phone").forEach { (id, label) ->
            FilterChip(selected = s.brainSource == id, onClick = { s.brainSource = id }, label = { Text(label) })
        }
    }
    OnPhoneHint(when (s.brainSource) {
        "server" -> "Always the server, as before. The on-phone brain is never used."
        "phone" -> "Always this phone, even when the server is there. Private and offline, but a much smaller brain: " +
            "no web search, projects or server tools."
        else -> "The server when it can be reached; this phone when it can't (no signal, server off). " +
            "Needs a model on the phone."
    })

    val budget = brain.budget

    // The model on the phone
    val m = brain.installed
    if (m != null) {
        Column {
            Text("${m.name} · ${ModelPicker.sizeText(m.size)}")
            val state = when (brain.engineState) {
                "ready" -> "Loaded and ready"
                "starting" -> "Loading…"
                "failed" -> "✗ Couldn't start: ${brain.engineError.ifBlank { "unknown problem" }}"
                else -> "Sleeping (loads on the next offline message)"
            }
            StatusLine(state, style = MaterialTheme.typography.bodySmall, color = Space.Muted, badColor = Space.Red)
            if (brain.engineState == "ready" && brain.safeMode) {
                OnPhoneHint("Running with lighter settings (remembers less of the chat) because the normal start was stopped.")
            }
        }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (brain.engineState != "ready" && brain.engineState != "starting") {
                OutlinedButton(onClick = {
                    scope.launch { try { brain.ensureRunning() } catch (_: Exception) { } }
                }) { Text("Test load") }
            }
            if (brain.engineState == "failed") {
                OutlinedButton(onClick = { brain.searchSmaller() }, enabled = !brain.searching && budget != null) {
                    Text("Find a smaller model")
                }
            }
            if (brain.engineState == "failed" || brain.safeMode) {
                TextButton(onClick = {
                    val cm = app.getSystemService(android.content.Context.CLIPBOARD_SERVICE) as android.content.ClipboardManager
                    cm.setPrimaryClip(android.content.ClipData.newPlainText("StellAI brain log", brain.logText()))
                    copied = true
                }) { Text(if (copied) "Log copied" else "Copy brain log") }
            }
        }
    } else {
        Text("No model on this phone yet.", color = Space.Muted)
    }

    // The phone
    when {
        !brain.hasProgram -> OnPhoneHint("✗ This build of StellAI doesn't include the on-phone brain program. Install the latest release.")
        budget == null -> OnPhoneHint("✗ " + ModelPicker.whyNot(brain.device))
        else -> OnPhoneHint(brain.deviceSummary())
    }

    // A download
    brain.download?.let { d ->
        Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text("${d.label}: ${ModelPicker.sizeText(d.done)} of ${ModelPicker.sizeText(d.total)}")
            LinearProgressIndicator(progress = { d.fraction }, modifier = Modifier.fillMaxWidth())
            StatusLine(if (d.error.isNotEmpty()) "✗ ${d.error}" else d.status,
                style = MaterialTheme.typography.bodySmall, color = Space.Muted, badColor = Space.Red)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (d.running) {
                    OutlinedButton(onClick = { brain.cancelDownload(discard = false) }) { Text("Pause") }
                } else {
                    val mobile = brain.onMobileData && !s.onPhoneMobileData
                    Button(onClick = { brain.resumeDownload(confirmedMobile = mobile) }) {
                        Text(if (mobile) "Resume on mobile data" else "Resume")
                    }
                }
                TextButton(onClick = { brain.cancelDownload(discard = true) }) { Text("Cancel") }
            }
        }
    }

    if (brain.hasProgram && budget != null) {
        Button(onClick = { brain.searchNow() }, enabled = !brain.searching && brain.download?.running != true,
            modifier = Modifier.fillMaxWidth()) {
            Text(if (m == null) "Find a model for this phone" else "Look for a better model now")
        }
    }
    if (brain.searchNote.isNotEmpty()) {
        StatusLine(brain.searchNote, style = MaterialTheme.typography.bodySmall, color = Space.Muted,
            okColor = Space.Accent, badColor = Space.Red)
    }

    if (brain.hasProgram) {
        // Loading one by hand
        OutlinedButton(onClick = { pickFile.launch(arrayOf("*/*")) }, enabled = brain.importing.isEmpty(),
            modifier = Modifier.fillMaxWidth()) {
            Text(brain.importing.ifEmpty { "Load a model file (.gguf)…" })
        }
        OutlinedTextField(
            value = link,
            onValueChange = { link = it; linkStatus = "" },
            label = { Text("Or paste a Hugging Face link") },
            placeholder = { Text("owner/model-GGUF or a .gguf file link") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
            trailingIcon = {
                TextButton(enabled = link.isNotBlank(), onClick = {
                    linkStatus = "Looking it up…"
                    scope.launch {
                        try {
                            val c = withContext(Dispatchers.IO) { brain.fromLink(link) }
                            linkStatus = ""
                            brain.offerChoice(c)
                        } catch (e: Exception) {
                            linkStatus = "✗ " + (e.message ?: "Couldn't use that link")
                        }
                    }
                }) { Text("Get") }
            },
        )
        if (linkStatus.isNotEmpty()) {
            StatusLine(linkStatus, style = MaterialTheme.typography.bodySmall, color = Space.Muted, badColor = Space.Red)
        }
        OnPhoneHint("Any GGUF chat model works. Bigger than the suggested size may be slow or not fit in memory.")
    }

    OnPhoneSpeechSettings()

    OnPhoneSwitch("Downloads may use mobile data",
        "Off: models only download on Wi-Fi (you can still OK one download at a time).", s.onPhoneMobileData) {
        s.onPhoneMobileData = it
    }
    OnPhoneSwitch("Look for better models",
        "About once a week, $stella checks Hugging Face for a better model for this phone and asks before downloading.",
        s.onPhoneAutoFind) { s.onPhoneAutoFind = it }

    if (m != null) {
        TextButton(onClick = { confirmRemove = true }) { Text("Remove the model from this phone", color = Space.Red) }
    }
    if (confirmRemove) {
        AlertDialog(
            onDismissRequest = { confirmRemove = false },
            text = { Text("Delete ${m?.name ?: "the model"} from this phone? You can download one again later.") },
            confirmButton = { TextButton(onClick = { brain.remove(); confirmRemove = false }) { Text("Delete") } },
            dismissButton = { TextButton(onClick = { confirmRemove = false }) { Text("Keep") } },
        )
    }
}

@Composable
private fun OnPhoneHint(text: String) {
    StatusLine(text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
        badColor = Space.Red)
}

@Composable
private fun OnPhoneSwitch(title: String, detail: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f).padding(end = 12.dp)) {
            Text(title)
            Text(detail, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Switch(checked = checked, onCheckedChange = onChange)
    }
}

/** Settings → On-phone brain → Speech: speech to text on the phone (English), for talking without the server. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun OnPhoneSpeechSettings() {
    val app = App.instance
    val s = app.settings
    val sp = app.speech
    var chosen by remember { mutableStateOf(sp.installed?.id ?: sp.recommended().id) }

    Text("Speech (English)")
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        listOf("server" to "Server", "auto" to "Auto", "phone" to "This phone").forEach { (id, label) ->
            FilterChip(selected = s.speechSource == id, onClick = { s.speechSource = id }, label = { Text(label) })
        }
    }
    OnPhoneHint(when (s.speechSource) {
        "server" -> "Your voice is always turned into text by the server, as before."
        "phone" -> "Your voice is always turned into text on this phone; nothing is sent to the server."
        else -> "The server turns your voice into text; this phone does it when the server can't be reached."
    })
    if (!sp.hasProgram) {
        OnPhoneHint("✗ This build of StellAI doesn't include on-phone speech. Install the latest release.")
        return
    }
    val inst = sp.installed
    Text(if (inst != null) "${inst.label} speech model on the phone" else "No speech model on the phone yet.",
        color = if (inst != null) MaterialTheme.colorScheme.onSurface else Space.Muted)
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OnPhoneSpeech.SIZES.forEach { size ->
            FilterChip(selected = chosen == size.id, onClick = { chosen = size.id },
                label = { Text("${size.label} · ${ModelPicker.sizeText(size.bytes)}") })
        }
    }
    OnPhoneSpeech.SIZES.firstOrNull { it.id == chosen }?.let { OnPhoneHint(it.label + ": " + it.detail + ".") }

    val d = sp.download
    if (d != null) {
        Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text("${d.label}: ${ModelPicker.sizeText(d.done)} of ${ModelPicker.sizeText(d.total)}")
            LinearProgressIndicator(progress = { d.fraction }, modifier = Modifier.fillMaxWidth())
            StatusLine(if (d.error.isNotEmpty()) "✗ ${d.error}" else d.status,
                style = MaterialTheme.typography.bodySmall, color = Space.Muted, badColor = Space.Red)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (d.running) {
                    OutlinedButton(onClick = { sp.cancelDownload(discard = false) }) { Text("Pause") }
                } else {
                    val mobile = app.onPhone.onMobileData && !s.onPhoneMobileData
                    Button(onClick = { sp.resumeDownload(confirmedMobile = mobile) }) {
                        Text(if (mobile) "Resume on mobile data" else "Resume")
                    }
                }
                TextButton(onClick = { sp.cancelDownload(discard = true) }) { Text("Cancel") }
            }
        }
    } else if (inst?.id != chosen) {
        val size = OnPhoneSpeech.SIZES.first { it.id == chosen }
        val mobile = app.onPhone.onMobileData && !s.onPhoneMobileData
        Button(onClick = { sp.startDownload(size, confirmedMobile = mobile) }, modifier = Modifier.fillMaxWidth()) {
            Text((if (inst == null) "Download" else "Switch to") + " ${size.label} (${ModelPicker.sizeText(size.bytes)})" +
                if (mobile) " on mobile data" else "")
        }
    }
    if (inst != null && d == null) {
        TextButton(onClick = { sp.remove() }) { Text("Remove the speech model", color = Space.Red) }
    }
}
