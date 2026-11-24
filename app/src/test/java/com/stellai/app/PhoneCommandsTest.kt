package com.stellai.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Simple phone commands the on-phone brain carries out without a model. */
class PhoneCommandsTest {
    private val now = java.time.LocalDateTime.of(2026, 11, 23, 20, 5)
    private fun p(t: String) = PhoneCommands.parse(t, "Stella", now)
    private fun call(t: String) = p(t)?.let { it.tool + " " + it.args }

    @Test
    fun openingApps() {
        assertEquals("open_app {\"app_name\":\"spotify\"}", call("Open Spotify"))
        assertEquals("open_app {\"app_name\":\"camera\"}", call("hey Stella, could you please launch the camera app?"))
        assertEquals("open_app {\"app_name\":\"stellar disc\"}", call("start stellar disc for me"))
        assertEquals("open_app {\"app_name\":\"youtube\"}", call("pull up youtube"))
        assertEquals("open_settings {\"page\":\"wifi\"}", call("open wi-fi settings"))
        assertEquals("open_url {\"url\":\"https://github.com\"}", call("go to github.com"))
        assertNull("not a command", p("open a can of worms about the weather"))
    }

    @Test
    fun timersAndAlarms() {
        assertEquals("set_timer {\"seconds\":300}", call("set a timer for 5 minutes"))
        assertEquals("set_timer {\"seconds\":600}", call("start a ten minute timer"))
        assertEquals("set_timer {\"seconds\":5400}", call("timer 1 hour 30 minutes"))
        assertEquals("set_timer {\"seconds\":1800}", call("timer for half an hour"))
        assertEquals("cancel_timer {}", call("stop the timer"))
        assertEquals("set_alarm {\"hour\":7,\"minute\":0}", call("set an alarm for 7"))
        assertEquals("set_alarm {\"hour\":18,\"minute\":45}", call("alarm at 6:45 pm"))
        assertEquals("set_alarm {\"hour\":0,\"minute\":30}", call("wake me up at 12:30 am"))
        assertEquals("cancel_alarm {}", call("cancel my alarm"))
        assertNull("no time given", p("set an alarm"))
    }

    @Test
    fun phoneControls() {
        assertEquals("set_flashlight {\"on\":true}", call("turn on the flashlight"))
        assertEquals("set_flashlight {\"on\":false}", call("torch off"))
        assertEquals("set_media_volume {\"percent\":40}", call("set the volume to 40%"))
        assertEquals("get_battery_status {}", call("what's my battery level?"))
        assertEquals("media_control {\"action\":\"pause\"}", call("pause the music"))
        assertEquals("media_control {\"action\":\"next\"}", call("skip this song"))
        assertEquals("media_control {\"action\":\"play\"}", call("play music"))
        assertEquals("play_music {\"query\":\"daft punk\"}", call("play some Daft Punk"))
        assertEquals("play_music {\"query\":\"midnight city\",\"app\":\"spotify\"}", call("play midnight city on spotify"))
    }

    @Test
    fun timeAndDate() {
        assertEquals("It's 8:05 PM.", p("what time is it?")?.reply)
        assertEquals("It's Monday, November 23, 2026.", p("What's the date")?.reply)
    }

    @Test
    fun everythingElseGoesToTheModel() {
        assertNull(p("why is the sky blue"))
        assertNull(p("tell me a joke about timers"))
        assertNull(p("what do you think about alarm clocks in general"))
        assertTrue(PhoneCommands.normalize("Please open Spotify, thanks!") == "open spotify")
    }
}
