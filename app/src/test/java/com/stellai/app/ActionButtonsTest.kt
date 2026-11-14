package com.stellai.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The Cancel/Undo buttons under phone actions: what's saved, shown, sent to Stella and done when pressed. */
class ActionButtonsTest {
    private val undoBrightness = ActionButton("Undo", "set_brightness", """{"percent":40}""")

    @Test
    fun buttonRoundTripsThroughTheSavedLine() {
        val saved = ActionButtons.attach("✓ Brightness set to 70%", undoBrightness)
        val (text, button) = ActionButtons.split(saved)
        assertEquals("✓ Brightness set to 70%", text)
        assertEquals(undoBrightness, button)
    }

    @Test
    fun linesWithoutAButtonAreUnchanged() {
        assertEquals("✓ Opened Spotify" to null, ActionButtons.split("✓ Opened Spotify"))
        assertEquals("✓ Opened Spotify", ActionButtons.attach("✓ Opened Spotify", null))
    }

    @Test
    fun pressingNotesWhatHappened() {
        val saved = ActionButtons.attach("✓ Brightness set to 70%", undoBrightness)
        assertEquals("✓ Brightness set to 70% · undone", ActionButtons.afterPress(saved, undoBrightness, "✓ Brightness set to 40%"))
        val timer = ActionButton("Cancel timer", "cancel_timer", "{}")
        assertEquals("✓ Timer started: 5m · cancelled",
            ActionButtons.afterPress(ActionButtons.attach("✓ Timer started: 5m", timer), timer, "✓ Asked the clock app to stop the timer"))
        assertEquals("✓ Timer started: 5m · couldn't cancel timer: No clock app found",
            ActionButtons.afterPress("✓ Timer started: 5m", timer, "✗ No clock app found"))
    }

    @Test
    fun timersAndAlarmsGetCancel() {
        assertEquals("cancel_timer", ActionButtons.forAction("set_timer", JSONObject().put("seconds", 300), null)?.tool)
        val alarm = ActionButtons.forAction("set_alarm", JSONObject().put("hour", 6).put("minute", 45), null)
        assertEquals("Cancel alarm", alarm?.label)
        assertEquals("6:45", JSONObject(alarm!!.args).getString("time"))
        assertNull(ActionButtons.forAction("set_alarm", JSONObject().put("hour", 30), null))
    }

    @Test
    fun settingsUndoToWhatTheyWere() {
        val vol = ActionButtons.forAction("set_media_volume", JSONObject().put("percent", 80),
            JSONObject().put("percent", 30).put("stream", "media"))
        assertEquals(30, JSONObject(vol!!.args).getInt("percent"))
        assertNull("no undo when nothing changed", ActionButtons.forAction("set_media_volume",
            JSONObject().put("percent", 30), JSONObject().put("percent", 30)))
        val autoBack = ActionButtons.forAction("set_brightness", JSONObject().put("percent", 70),
            JSONObject().put("auto", true).put("percent", 50))
        assertTrue(JSONObject(autoBack!!.args).getBoolean("auto"))
        val light = ActionButtons.forAction("set_flashlight", JSONObject().put("on", true), null)
        assertEquals(false, JSONObject(light!!.args).getBoolean("on"))
        assertNull(ActionButtons.forAction("open_app", JSONObject(), null))
    }

    @Test
    fun soundModeUndo() {
        val ringing = JSONObject().put("ringer", "normal").put("dnd", false)
        assertEquals("normal", ActionButtons.soundUndo("silent", ringing))
        assertEquals("dnd_off", ActionButtons.soundUndo("dnd_on", ringing))
        assertNull(ActionButtons.soundUndo("normal", ringing))
        assertEquals("dnd_on", ActionButtons.soundUndo("dnd_off", JSONObject().put("ringer", "normal").put("dnd", true)))
        assertNull(ActionButtons.soundUndo("silent", null))
    }
}
