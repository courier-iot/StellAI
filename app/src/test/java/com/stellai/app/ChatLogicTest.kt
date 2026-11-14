package com.stellai.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Which phone actions are offered, error messages, and document references. */
class ChatLogicTest {
    @Test
    fun phoneActionsFollowTheMessage() {
        val timer = PhoneToolPicker.pick("set a timer for 5 minutes", fromVoice = false)!!
        assertTrue("set_timer" in timer && "cancel_timer" in timer)
        assertTrue("set_brightness" in PhoneToolPicker.pick("make the screen dimmer, it's too bright", false)!!)
        assertTrue(PhoneToolPicker.pick("what's the capital of France?", false)!!.isEmpty())
        assertNull("voice gets every action", PhoneToolPicker.pick("anything", fromVoice = true))
    }

    @Test
    fun everyPhoneActionCanBeOffered() {
        val defs = PhoneTools.definitions()
        val names = (0 until defs.length()).map { defs.getJSONObject(it).getJSONObject("function").getString("name") }
        // web_search on the phone is only a backup for servers without their own search
        val missing = names.filter { it !in PhoneToolPicker.TRIGGERS && it != "web_search" }
        assertTrue("phone actions no message words can bring up: $missing", missing.isEmpty())
    }

    @Test
    fun errorsSayWhatToDo() {
        assertTrue(friendlyErrorText(java.net.ConnectException("refused"), "http://x:8092").contains("Can't reach the server at http://x:8092"))
        assertTrue(friendlyErrorText(Exception("HTTP 404: not found"), "").contains("Check for updates"))
        assertTrue(friendlyErrorText(java.net.SocketTimeoutException("connect timed out"), "http://x").contains("Tailscale"))
        assertEquals("The server took too long to respond.", friendlyErrorText(java.net.SocketTimeoutException("timeout"), ""))
        assertTrue(friendlyErrorText(Exception("HTTP 401"), "").contains("API key"))
        assertEquals("boom", friendlyErrorText(Exception("boom"), ""))
        val old = listOf("re-run the setup script", "out of date")
        val all = listOf(friendlyErrorText(Exception("HTTP 404"), ""), friendlyErrorText(Exception("HTTP 401"), ""))
        assertTrue(all.none { m -> old.any { m.contains(it, ignoreCase = true) } })
    }

    @Test
    fun documentReferences() {
        val text = "Chapter 1 is beside the chat [doc:0123456789]."
        assertEquals("0123456789", DOC_REF.find(text)?.groupValues?.get(1))
        assertNull(DOC_REF.find("no reference [doc:xyz]"))
    }
}
