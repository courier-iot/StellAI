package com.stellai.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Reading Stella's instructions and modes as the server sends them (GET /prompts). */
class ModesTest {
    private val payload = JSONObject("""
        {"instructions": {"phone": "You are {name}. Phone line.", "web": "You are {name}."},
         "mode_note": "Mode first.", "summary_intro": "Summarized.", "voice_style": "Speak.",
         "modes": [{"id": "general", "name": "General", "icon": "✦", "desc": "Everyday", "prompt": "", "temperature": null,
                    "thinking": "default", "tools": "all", "style": "work"},
                   {"id": "writer", "name": "Writer", "icon": "✒", "desc": "Stories", "prompt": "Mode: Writer.",
                    "temperature": 1.0, "thinking": "off", "tools": "minimal", "style": "story"}]}
    """.trimIndent())

    @Test
    fun instructionsForThePhone() {
        val p = Prompts.from(payload)!!
        assertEquals("You are {name}. Phone line.", p.instructions)
        assertEquals("Speak.", p.voiceStyle)
        assertNull("no phone text: not usable", Prompts.from(JSONObject().put("instructions", JSONObject())))
    }

    @Test
    fun builtInModes() {
        val arr = payload.getJSONArray("modes")
        val general = Mode.fromJson(arr.getJSONObject(0), builtIn = true)
        val writer = Mode.fromJson(arr.getJSONObject(1), builtIn = true)
        assertNull(general.temperature)
        assertEquals(1.0, writer.temperature!!, 0.0)
        assertEquals("Stories", writer.description)
        assertTrue(writer.builtIn && writer.tools == "minimal" && writer.style == "story")
        assertEquals("Your mode", Mode.fromJson(arr.getJSONObject(1)).description)
    }
}
