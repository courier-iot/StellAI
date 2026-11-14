package com.stellai.app

import org.json.JSONObject

/** A button under a phone action's result line: Cancel a timer or alarm she just set, or Undo a setting change. */
data class ActionButton(val label: String, val tool: String, val args: String)

/**
 * The button is kept in the saved result line as a marker at its end ("[[button:Undo|set_brightness|{…}]]"), so it
 * syncs with the chat. The phone draws it as a button; the desktop chat and Stella's own history leave it out.
 * Pressing it runs the phone action, then the marker is replaced by what happened.
 */
object ActionButtons {
    private val MARK = Regex("""\n?\[\[button:([^|\]\n]+)\|([a-z_]+)\|(\{[^\n]*?\})]]\s*$""")

    fun attach(summary: String, button: ActionButton?): String =
        if (button == null) summary else "$summary\n[[button:${button.label}|${button.tool}|${button.args}]]"

    /** The line without its marker, and the button (null if there is none). */
    fun split(text: String): Pair<String, ActionButton?> {
        val m = MARK.find(text) ?: return text to null
        val (label, tool, args) = m.destructured
        return text.removeRange(m.range).trimEnd() to ActionButton(label, tool, args)
    }

    fun strip(text: String): String = split(text).first

    /** The line after the button was pressed: the marker goes, and a short note says what happened. */
    fun afterPress(text: String, button: ActionButton, resultSummary: String): String {
        val base = strip(text)
        val ok = resultSummary.startsWith("✓")
        val done = if (button.label.startsWith("Cancel")) "cancelled" else "undone"
        return if (ok) "$base · $done"
        else "$base · couldn't ${button.label.lowercase()}: ${resultSummary.removePrefix("✗").trim()}"
    }

    /**
     * The button for an action that worked. [before] = the setting's value just before the change (from
     * PhoneExtras.snapshot), needed to undo it. No button when there's nothing sensible to undo.
     */
    fun forAction(tool: String, args: JSONObject, before: JSONObject?): ActionButton? = when (tool) {
        "set_timer" -> ActionButton("Cancel timer", "cancel_timer", "{}")
        "set_alarm" -> {
            val h = args.optInt("hour", -1)
            val m = args.optInt("minute", 0)
            if (h in 0..23 && m in 0..59) ActionButton("Cancel alarm", "cancel_alarm",
                JSONObject().put("time", "%d:%02d".format(h, m)).toString()) else null
        }
        "set_flashlight" -> ActionButton("Undo", "set_flashlight",
            JSONObject().put("on", !args.optBoolean("on", true)).toString())
        "set_media_volume" -> before?.takeIf { it.has("percent") && it.optInt("percent") != args.optInt("percent", -1) }
            ?.let { ActionButton("Undo", "set_media_volume",
                JSONObject().put("percent", it.optInt("percent")).put("stream", it.optString("stream", "media")).toString()) }
        "set_brightness" -> when {
            before == null -> null
            before.optBoolean("auto") && !args.optBoolean("auto") ->
                ActionButton("Undo", "set_brightness", JSONObject().put("auto", true).toString())
            before.has("percent") && !before.optBoolean("auto") ->
                ActionButton("Undo", "set_brightness", JSONObject().put("percent", before.optInt("percent")).toString())
            else -> null
        }
        "set_sound_mode" -> soundUndo(args.optString("mode"), before)?.let {
            ActionButton("Undo", "set_sound_mode", JSONObject().put("mode", it).toString())
        }
        else -> null
    }

    /** The mode that puts sound back: Do Not Disturb as it was, or the ringer (normal, vibrate, silent) as it was. */
    fun soundUndo(newMode: String, before: JSONObject?): String? {
        if (before == null) return null
        val mode = newMode.lowercase().replace(' ', '_')
        val dnd = before.optBoolean("dnd")
        val ringer = before.optString("ringer")
        return when (mode) {
            "dnd_on", "do_not_disturb", "dnd" -> if (dnd) null else "dnd_off"
            "dnd_off" -> if (dnd) "dnd_on" else null
            "silent", "vibrate", "normal", "ring", "sound" -> {
                val now = if (mode == "ring" || mode == "sound") "normal" else mode
                ringer.takeIf { it in setOf("normal", "vibrate", "silent") && it != now }
            }
            else -> null
        }
    }
}
