package com.localai.remote

import android.content.Context
import android.content.Intent
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.media.AudioManager
import android.net.Uri
import android.os.BatteryManager
import android.provider.AlarmClock
import android.provider.CalendarContract
import org.json.JSONArray
import org.json.JSONObject
import java.time.LocalDateTime
import java.time.ZoneId

/**
 * Phone actions the AI on the laptop can ask for.
 * Anything that sends or calls (texts, phone calls, calendar events) only opens
 * the right screen pre-filled, so you always press the final button yourself.
 *
 * To add a tool: add a definition in [definitions] and a branch in [execute].
 */
object PhoneTools {

    data class Outcome(val summary: String, val result: String)

    fun definitions(): JSONArray = JSONArray().apply {
        put(tool("set_alarm", "Set an alarm on the phone's clock app.",
            props(
                "hour" to prop("integer", "Hour in 24-hour time, 0-23"),
                "minute" to prop("integer", "Minute, 0-59"),
                "label" to prop("string", "Optional alarm label"),
            ), "hour", "minute"))
        put(tool("set_timer", "Start a countdown timer.",
            props(
                "seconds" to prop("integer", "Timer length in seconds"),
                "label" to prop("string", "Optional timer label"),
            ), "seconds"))
        put(tool("open_app", "Open an installed app by its name, e.g. 'Spotify' or 'Camera'.",
            props("app_name" to prop("string", "The app's name as shown on the home screen")),
            "app_name"))
        put(tool("open_url", "Open a web page in the browser.",
            props("url" to prop("string", "Full URL starting with https://")), "url"))
        put(tool("web_search", "Search the web on the phone.",
            props("query" to prop("string", "What to search for")), "query"))
        put(tool("navigate_to", "Open maps with directions to a place or address.",
            props("destination" to prop("string", "Place name or address")), "destination"))
        put(tool("draft_text_message",
            "Open the messaging app with a text pre-written. The user presses send.",
            props(
                "message" to prop("string", "The message text"),
                "phone_number" to prop("string", "Optional recipient phone number"),
            ), "message"))
        put(tool("dial_number", "Open the phone dialer with a number filled in. The user presses call.",
            props("phone_number" to prop("string", "Phone number to dial")), "phone_number"))
        put(tool("create_calendar_event",
            "Open the calendar with a new event filled in. The user presses save.",
            props(
                "title" to prop("string", "Event title"),
                "start" to prop("string", "Start in the format YYYY-MM-DD HH:MM (24-hour)"),
                "duration_minutes" to prop("integer", "Length in minutes, default 60"),
                "location" to prop("string", "Optional location"),
            ), "title", "start"))
        put(tool("set_flashlight", "Turn the flashlight on or off.",
            props("on" to prop("boolean", "true to turn on, false to turn off")), "on"))
        put(tool("set_media_volume", "Set the phone's media volume.",
            props("percent" to prop("integer", "Volume from 0 to 100")), "percent"))
        put(tool("get_battery_status", "Get the phone's battery level and charging state.", props()))
    }

    fun displayName(name: String): String = when (name) {
        "set_alarm" -> "Setting alarm"
        "set_timer" -> "Starting timer"
        "open_app" -> "Opening app"
        "open_url" -> "Opening page"
        "web_search" -> "Searching"
        "navigate_to" -> "Opening maps"
        "draft_text_message" -> "Drafting text"
        "dial_number" -> "Opening dialer"
        "create_calendar_event" -> "Creating event"
        "set_flashlight" -> "Flashlight"
        "set_media_volume" -> "Setting volume"
        "get_battery_status" -> "Checking battery"
        else -> name
    }

    /** Must be called on the main thread. */
    fun execute(ctx: Context, name: String, argsJson: String): Outcome {
        val args = try {
            JSONObject(argsJson.ifBlank { "{}" })
        } catch (e: Exception) {
            JSONObject()
        }
        return try {
            when (name) {
                "set_alarm" -> {
                    val hour = args.optInt("hour", -1)
                    val minute = args.optInt("minute", 0)
                    if (hour !in 0..23 || minute !in 0..59) return fail("Invalid time")
                    val label = args.optString("label", "")
                    val intent = Intent(AlarmClock.ACTION_SET_ALARM)
                        .putExtra(AlarmClock.EXTRA_HOUR, hour)
                        .putExtra(AlarmClock.EXTRA_MINUTES, minute)
                        .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                    if (label.isNotBlank()) intent.putExtra(AlarmClock.EXTRA_MESSAGE, label)
                    val time = "%d:%02d".format(hour, minute)
                    launch(ctx, intent, "Alarm set for $time", "No clock app found")
                }
                "set_timer" -> {
                    val seconds = args.optInt("seconds", 0)
                    if (seconds <= 0) return fail("Invalid timer length")
                    val label = args.optString("label", "")
                    val intent = Intent(AlarmClock.ACTION_SET_TIMER)
                        .putExtra(AlarmClock.EXTRA_LENGTH, seconds)
                        .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                    if (label.isNotBlank()) intent.putExtra(AlarmClock.EXTRA_MESSAGE, label)
                    launch(ctx, intent, "Timer started: ${formatDuration(seconds)}", "No clock app found")
                }
                "open_app" -> openApp(ctx, args.optString("app_name", ""))
                "open_url" -> {
                    var url = args.optString("url", "").trim()
                    if (url.isEmpty()) return fail("No URL given")
                    if (!url.startsWith("http://") && !url.startsWith("https://")) url = "https://$url"
                    launch(ctx, Intent(Intent.ACTION_VIEW, Uri.parse(url)), "Opened $url", "No browser found")
                }
                "web_search" -> {
                    val q = args.optString("query", "").trim()
                    if (q.isEmpty()) return fail("No search query")
                    val uri = Uri.parse("https://duckduckgo.com/?q=" + Uri.encode(q))
                    launch(ctx, Intent(Intent.ACTION_VIEW, uri), "Searched for \"$q\"", "No browser found")
                }
                "navigate_to" -> {
                    val dest = args.optString("destination", "").trim()
                    if (dest.isEmpty()) return fail("No destination")
                    val uri = Uri.parse("geo:0,0?q=" + Uri.encode(dest))
                    launch(ctx, Intent(Intent.ACTION_VIEW, uri), "Opened maps to $dest", "No maps app found")
                }
                "draft_text_message" -> {
                    val msg = args.optString("message", "")
                    val number = args.optString("phone_number", "").trim()
                    val intent = Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + Uri.encode(number)))
                        .putExtra("sms_body", msg)
                    launch(ctx, intent, "Text drafted (waiting for you to send)", "No messaging app found")
                }
                "dial_number" -> {
                    val number = args.optString("phone_number", "").trim()
                    if (number.isEmpty()) return fail("No phone number")
                    val intent = Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(number)))
                    launch(ctx, intent, "Dialer opened with $number", "No dialer found")
                }
                "create_calendar_event" -> createEvent(ctx, args)
                "set_flashlight" -> setFlashlight(ctx, args.optBoolean("on", true))
                "set_media_volume" -> {
                    val percent = args.optInt("percent", -1)
                    if (percent !in 0..100) return fail("Volume must be 0-100")
                    val audio = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
                    val max = audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
                    val level = Math.round(max * percent / 100f)
                    audio.setStreamVolume(AudioManager.STREAM_MUSIC, level, AudioManager.FLAG_SHOW_UI)
                    ok("Media volume set to $percent%")
                }
                "get_battery_status" -> {
                    val bm = ctx.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
                    val level = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
                    val charging = bm.isCharging
                    ok("Battery $level%" + if (charging) ", charging" else ", not charging")
                }
                else -> fail("Unknown tool: $name")
            }
        } catch (e: Exception) {
            fail(e.message ?: e.javaClass.simpleName)
        }
    }

    // ---- Tool implementations -------------------------------------------------

    private fun openApp(ctx: Context, rawName: String): Outcome {
        val query = rawName.trim().lowercase()
        if (query.isEmpty()) return fail("No app name")
        val pm = ctx.packageManager
        val launcherIntent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        @Suppress("DEPRECATION")
        val apps = pm.queryIntentActivities(launcherIntent, 0)
            .map { it.loadLabel(pm).toString() to it.activityInfo.packageName }

        val match = apps.firstOrNull { it.first.lowercase() == query }
            ?: apps.firstOrNull { it.first.lowercase().startsWith(query) }
            ?: apps.firstOrNull { it.first.lowercase().contains(query) }
            ?: apps.firstOrNull { it.first.length >= 3 && query.contains(it.first.lowercase()) }
            ?: return fail("No app called \"$rawName\" is installed")

        val intent = pm.getLaunchIntentForPackage(match.second)
            ?: return fail("Couldn't open ${match.first}")
        return launch(ctx, intent, "Opened ${match.first}", "Couldn't open ${match.first}")
    }

    private fun createEvent(ctx: Context, args: JSONObject): Outcome {
        val title = args.optString("title", "").ifBlank { "Event" }
        val startText = args.optString("start", "").trim().replace(' ', 'T')
        val start = try {
            LocalDateTime.parse(startText)
        } catch (e: Exception) {
            return fail("Start time must look like 2026-10-29 15:00")
        }
        val minutes = args.optInt("duration_minutes", 60).coerceAtLeast(1)
        val zone = ZoneId.systemDefault()
        val begin = start.atZone(zone).toInstant().toEpochMilli()
        val end = start.plusMinutes(minutes.toLong()).atZone(zone).toInstant().toEpochMilli()
        val intent = Intent(Intent.ACTION_INSERT)
            .setData(CalendarContract.Events.CONTENT_URI)
            .putExtra(CalendarContract.EXTRA_EVENT_BEGIN_TIME, begin)
            .putExtra(CalendarContract.EXTRA_EVENT_END_TIME, end)
            .putExtra(CalendarContract.Events.TITLE, title)
        val location = args.optString("location", "")
        if (location.isNotBlank()) intent.putExtra(CalendarContract.Events.EVENT_LOCATION, location)
        return launch(ctx, intent, "Calendar opened with \"$title\" (waiting for you to save)", "No calendar app found")
    }

    private fun setFlashlight(ctx: Context, on: Boolean): Outcome {
        val cm = ctx.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val id = cm.cameraIdList.firstOrNull {
            cm.getCameraCharacteristics(it).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
        } ?: return fail("This phone has no flashlight")
        cm.setTorchMode(id, on)
        return ok(if (on) "Flashlight on" else "Flashlight off")
    }

    // ---- Helpers ----------------------------------------------------------------

    private fun launch(ctx: Context, intent: Intent, success: String, failure: String): Outcome =
        try {
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(intent)
            ok(success)
        } catch (e: Exception) {
            fail(failure)
        }

    private fun ok(msg: String) =
        Outcome("✓ $msg", JSONObject().put("ok", true).put("result", msg).toString())

    private fun fail(msg: String) =
        Outcome("✗ $msg", JSONObject().put("ok", false).put("error", msg).toString())

    private fun formatDuration(seconds: Int): String {
        val h = seconds / 3600
        val m = (seconds % 3600) / 60
        val s = seconds % 60
        return listOfNotNull(
            if (h > 0) "${h}h" else null,
            if (m > 0) "${m}m" else null,
            if (s > 0) "${s}s" else null,
        ).joinToString(" ")
    }

    private fun prop(type: String, description: String) =
        JSONObject().put("type", type).put("description", description)

    private fun props(vararg pairs: Pair<String, JSONObject>) =
        JSONObject().apply { pairs.forEach { (k, v) -> put(k, v) } }

    private fun tool(name: String, description: String, properties: JSONObject, vararg required: String) =
        JSONObject()
            .put("type", "function")
            .put("function", JSONObject()
                .put("name", name)
                .put("description", description)
                .put("parameters", JSONObject()
                    .put("type", "object")
                    .put("properties", properties)
                    .put("required", JSONArray(required.toList()))))
}
