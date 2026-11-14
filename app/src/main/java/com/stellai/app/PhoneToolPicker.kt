package com.stellai.app

/**
 * Which phone actions Stella is offered for a message: only those whose words appear in it, so the list sent to the
 * model stays short. Voice requests (null) get them all, because speech is often phrased loosely.
 */
object PhoneToolPicker {
    val TRIGGERS = mapOf(
        "set_alarm" to Regex("alarm|wake me"),
        "set_timer" to Regex("timer|countdown|count down"),
        "open_app" to Regex("\\bopen\\b|launch|\\bstart\\b|\\bplay\\b|\\bapp\\b|spotify|youtube|camera"),
        "open_url" to Regex("https?://|website|\\burl\\b|\\blink\\b|go to "),
        "navigate_to" to Regex("navigat|direction|drive to|route|\\bmaps?\\b|take me|how do i get"),
        "draft_text_message" to Regex("\\btext\\b|\\bsms\\b|message (to|for)|send .* (a )?message|tell \\w+ that"),
        "dial_number" to Regex("\\bcall\\b|\\bdial\\b|phone (number|him|her|them)"),
        "create_calendar_event" to Regex("calendar|event|meeting|appointment|schedule"),
        "set_flashlight" to Regex("flash ?light|torch|\\blight\\b"),
        "set_media_volume" to Regex("volume|louder|quieter|\\bmute|turn (it )?(up|down)"),
        "get_battery_status" to Regex("battery|charg(e|ing) level|how much charge"),
        "cancel_alarm" to Regex("alarm"),
        "alarm_info" to Regex("alarm"),
        "open_clock" to Regex("alarm|timer"),
        "cancel_timer" to Regex("timer"),
        "media_control" to Regex("\\bplay|pause|resume|skip|next (song|track)|previous|\\bsong|track|music|podcast|stop (the )?(music|song|video)"),
        "now_playing" to Regex("\\bsong|track|playing|music|listening|what'?s this"),
        "play_music" to Regex("\\bplay\\b|\\bsong|music|album|playlist|artist|put on"),
        "uplink_download" to Regex("download|uplink|save (this|that|the) (song|video|track)|grab (this|that|the)"),
        "set_sound_mode" to Regex("silent|vibrat|do not disturb|\\bdnd\\b|ringer|\\bmute|sound (on|off)|quiet mode"),
        "set_brightness" to Regex("bright|\\bdim\\b|darker|screen light"),
        "open_settings" to Regex("wi-?fi|bluetooth|mobile data|airplane|flight mode|hotspot|\\bnfc\\b|location|\\bgps\\b|settings"),
        "read_notifications" to Regex("notification|\\bmessages?\\b|e-?mails?|whatsapp|unread|anyone (text|message)|did .* (text|message)"),
        "find_contact" to Regex("\\bcall\\b|\\btext\\b|\\bnumber|contact|message (to|for)"),
        "upcoming_events" to Regex("calendar|schedule|appointment|meeting|what'?s on|agenda|\\bbusy\\b|free (on|this|tomorrow|today)"),
    )

    fun pick(text: String, fromVoice: Boolean): Set<String>? {
        if (fromVoice) return null
        val t = text.lowercase()
        return TRIGGERS.filterValues { it.containsMatchIn(t) }.keys
    }
}
