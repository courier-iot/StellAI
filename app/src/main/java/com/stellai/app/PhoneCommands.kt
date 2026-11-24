package com.stellai.app

/**
 * Simple phone commands understood without a model ("open Spotify", "timer 5 minutes", "alarm at 7:30",
 * "flashlight on", "pause the music"…). Used by the on-phone brain: small models are unreliable at choosing and
 * filling in actions, and these don't need a model at all, so they run at once (even before the brain loads).
 * No Android code here, so the tests can check it.
 */
object PhoneCommands {

    /** A phone action ([tool] with JSON [args], as in [PhoneTools]) or a plain [reply] with no action. */
    data class Command(val tool: String = "", val args: String = "{}", val reply: String = "")

    private val NUMBERS = mapOf(
        "a" to 1, "an" to 1, "one" to 1, "two" to 2, "three" to 3, "four" to 4, "five" to 5, "six" to 6, "seven" to 7,
        "eight" to 8, "nine" to 9, "ten" to 10, "fifteen" to 15, "twenty" to 20, "thirty" to 30, "forty" to 40,
        "forty-five" to 45, "sixty" to 60, "ninety" to 90,
    )

    /** A JSON string: "say \"hi\"". */
    private fun q(s: String) = "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\""

    /** Turns "Hey Stella, could you please open Spotify for me?" into "open spotify". */
    fun normalize(text: String, assistantName: String = "stella"): String {
        var t = text.trim().lowercase().replace(Regex("\\s+"), " ")
        t = t.replace(Regex("[.!?]+$"), "").trim()
        val name = Regex.escape(assistantName.lowercase().ifBlank { "stella" })
        repeat(3) {
            t = t.replace(Regex("^(hey |hi |ok |okay )?$name[,:]? "), "")
                .replace(Regex("^(please |pls |can you |could you |would you |will you |i want you to |i'd like you to |go ahead and |just )"), "")
                .trim()
        }
        t = t.replace(Regex(",? (please|pls|for me|thanks|thank you)$"), "").trim()
        return t
    }

    /** The command in [text], or null if it isn't one of the simple ones (then the model answers). */
    fun parse(text: String, assistantName: String = "stella", now: java.time.LocalDateTime = java.time.LocalDateTime.now()): Command? {
        val t = normalize(text, assistantName)
        if (t.isEmpty() || t.length > 80) return null

        // the time and date
        if (Regex("^(what'?s the time|what time is it|what is the time|time\\??|tell me the time)( now| right now)?$").matches(t)) {
            val h = now.hour % 12
            return Command(reply = "It's ${if (h == 0) 12 else h}:${"%02d".format(now.minute)} ${if (now.hour < 12) "AM" else "PM"}.")
        }
        if (Regex("^(what'?s the date|what is the date|what'?s today'?s date|what day is it|what is today|what'?s today)( today)?$").matches(t)) {
            val f = java.time.format.DateTimeFormatter.ofPattern("EEEE, MMMM d, yyyy", java.util.Locale.ENGLISH)
            return Command(reply = "It's ${now.format(f)}.")
        }

        // timers: "timer for 5 minutes", "set a 10 minute timer", "start a timer for 1 hour 30 minutes"
        if (Regex("\\btimer\\b").containsMatchIn(t)) {
            if (Regex("^(cancel|stop|end|turn off|dismiss)( the| my)? timer$").matches(t)) return Command("cancel_timer")
            val secs = durationSeconds(t)
            if (secs != null && secs > 0) return Command("set_timer", "{\"seconds\":$secs}")
            return null
        }

        // alarms: "alarm for 7", "set an alarm at 6:45 am", "wake me up at 7:30"
        if (Regex("\\balarm\\b|^wake me").containsMatchIn(t)) {
            if (Regex("^(cancel|turn off|delete|remove|stop)( the| my)? (next )?alarm$").matches(t)) return Command("cancel_alarm")
            val m = Regex("(?:for|at) (\\d{1,2})(?::(\\d{2}))? ?(am|pm|a\\.m|p\\.m)?(?: tomorrow| tonight| in the morning)?$").find(t)
                ?: return null
            var hour = m.groupValues[1].toInt()
            val minute = m.groupValues[2].ifEmpty { "0" }.toInt()
            val ampm = m.groupValues[3]
            if (hour > 23 || minute > 59) return null
            if (ampm.startsWith("p") && hour < 12) hour += 12
            if (ampm.startsWith("a") && hour == 12) hour = 0
            if (ampm.isEmpty() && Regex("tonight").containsMatchIn(t) && hour < 12) hour += 12
            return Command("set_alarm", "{\"hour\":$hour,\"minute\":$minute}")
        }

        // flashlight
        Regex("^(?:turn |switch )?(on|off) (?:the |my )?(?:flash ?light|torch)$|^(?:turn |switch )?(?:the |my )?(?:flash ?light|torch) (on|off)$")
            .find(t)?.let { m ->
                val on = (m.groupValues[1] + m.groupValues[2]) == "on"
                return Command("set_flashlight", "{\"on\":$on}")
            }

        // volume: "volume 50", "set the volume to 30%"
        Regex("^(?:set |turn )?(?:the )?(?:media )?volume (?:to |at )?(\\d{1,3})(?: ?%| percent)?$").find(t)?.let { m ->
            val p = m.groupValues[1].toInt()
            if (p in 0..100) return Command("set_media_volume", "{\"percent\":$p}")
        }

        // battery
        if (Regex("^(?:what'?s |what is |how'?s |check )?(?:my |the )?battery(?: level| status| life| percentage)?$|^how much (?:battery|charge)").containsMatchIn(t)) {
            return Command("get_battery_status")
        }

        // music that's playing
        val media = mapOf(
            "^(pause|stop)( the| my)?( music| song| video| playback)?$" to "pause",
            "^(resume|unpause|continue)( the| my)?( music| song| video| playback)?$" to "play",
            "^play( the)?( music)?$" to "play",
            "^(next|skip)( song| track| this( song| track)?)?$|^skip( to the)? next( song| track)?$|^play the next (song|track)$" to "next",
            "^(previous|last|go back)( song| track)?$|^play the (previous|last) (song|track)$" to "previous",
        )
        for ((re, action) in media) if (Regex(re).matches(t)) return Command("media_control", "{\"action\":${q(action)}}")

        // "play bohemian rhapsody", "play some daft punk"
        Regex("^play (?:some |me |the song |the album |the playlist |my )?(.{2,60}?)(?: on (spotify|youtube music|stellar disc))?$").find(t)?.let { m ->
            val what = m.groupValues[1].trim()
            if (what !in setOf("music", "a song", "something", "anything")) {
                val app = m.groupValues[2]
                return Command("play_music", "{\"query\":${q(what)}" + (if (app.isNotEmpty()) ",\"app\":${q(app)}" else "") + "}")
            }
        }

        // settings pages
        Regex("^(?:open|show|go to)(?: the| my)? (wi-?fi|bluetooth|location|hotspot|nfc|display|battery|airplane mode|airplane|mobile data|internet)(?: settings| page)?$")
            .find(t)?.let { m ->
                val page = when (val p = m.groupValues[1].replace("-", "")) {
                    "airplane mode" -> "airplane"
                    "mobile data" -> "internet"
                    else -> p
                }
                return Command("open_settings", "{\"page\":${q(page)}}")
            }

        // websites
        Regex("^(?:open|go to|visit) (https?://\\S+|[a-z0-9-]+(?:\\.[a-z0-9-]+)*\\.(?:com|org|net|io|ca|co|dev|app|gg|tv|me)(?:/\\S*)?)$")
            .find(t)?.let { m ->
                val u = m.groupValues[1]
                return Command("open_url", "{\"url\":${q(if (u.startsWith("http")) u else "https://$u")}}")
            }

        // apps: "open spotify", "launch the camera app", "start youtube"
        Regex("^(?:open|launch|start|run|bring up|pull up)(?: up)? (?:the |my )?(.{2,40}?)(?: app| application)?$").find(t)?.let { m ->
            val name = m.groupValues[1].trim()
            if (name.isNotEmpty() && !Regex("^(a |an |it$|that$|this$|settings$)").containsMatchIn(name)) {
                return Command("open_app", "{\"app_name\":${q(name)}}")
            }
        }
        return null
    }

    /** "1 hour 30 minutes" -> 5400, "ten minute" -> 600, "90 seconds" -> 90, "half an hour" -> 1800. */
    fun durationSeconds(t: String): Int? {
        if (Regex("half an? hour").containsMatchIn(t)) return 1800
        var total = 0
        var found = false
        val re = Regex("(\\d+(?:\\.\\d+)?|forty-five|${NUMBERS.keys.joinToString("|")})[ -]?(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\\b")
        for (m in re.findAll(t)) {
            val n = m.groupValues[1].toDoubleOrNull() ?: NUMBERS[m.groupValues[1]]?.toDouble() ?: continue
            val unit = m.groupValues[2]
            total += when {
                unit.startsWith("h") -> (n * 3600).toInt()
                unit.startsWith("m") -> (n * 60).toInt()
                else -> n.toInt()
            }
            found = true
        }
        return if (found) total else null
    }
}
