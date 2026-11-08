package com.stellai.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat

/**
 * Keeps StellAI running when you switch to another app, so a reply that's on its way still arrives,
 * sync and reminders keep working, and (when it's turned on) the wake word keeps listening.
 *
 * Without this, Android freezes the app a few seconds after it leaves the screen, which drops the
 * connection to the server. Android requires a visible notification for it. It can only be started
 * while the app is on screen (Android 14+ rule for microphone services), so the app starts it then.
 */
class AssistantService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val app = application as App
        if (intent?.action == ACTION_STOP) {
            app.settings.wakeWordEnabled = false
            app.settings.stayConnected = false
            stopSelf()
            return START_NOT_STICKY
        }
        // Every start re-checks what's needed (settings may have changed), and must call startForeground.
        var wake = app.settings.wakeWordEnabled && app.voice.hasMicPermission()
        if (!goForeground(app, wake)) {
            // The microphone part can be refused (e.g. started from the background): keep the connection anyway
            if (!wake || !goForeground(app, false)) {
                stopSelf()
                return START_NOT_STICKY
            }
            wake = false
        }
        if (wake) {
            // Keep the CPU awake so audio keeps flowing with the screen off.
            if (wakeLock?.isHeld != true) wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "StellAI:wakeword")
                .apply { setReferenceCounted(false); acquire() }
        } else {
            wakeLock?.let { if (it.isHeld) it.release() }
            wakeLock = null
        }
        app.voice.setWakeMode(wake)
        app.onAssistantServiceChanged(true, wake)
        // Not sticky: Android won't allow restarting a microphone service from the background anyway.
        return START_NOT_STICKY
    }

    private fun goForeground(app: App, wake: Boolean): Boolean = try {
        val types = when {
            Build.VERSION.SDK_INT >= 34 -> ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE or
                (if (wake) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0)
            Build.VERSION.SDK_INT >= 30 && wake -> ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE or
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            Build.VERSION.SDK_INT >= 29 -> ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            else -> 0
        }
        ServiceCompat.startForeground(this, NOTIFICATION_ID, buildNotification(app, wake), types)
        true
    } catch (e: Exception) {
        Log.e(TAG, "couldn't start in foreground (wake=$wake)", e)
        false
    }

    override fun onDestroy() {
        val app = application as App
        app.voice.setWakeMode(false)
        app.onAssistantServiceChanged(false, false)
        wakeLock?.let { if (it.isHeld) it.release() }
        super.onDestroy()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(CHANNEL_ID, "Running in the background", NotificationManager.IMPORTANCE_LOW)
            channel.description = "Keeps Stella connected to your server while you use other apps"
            channel.setShowBadge(false)
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }

    private fun buildNotification(app: App, wake: Boolean): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, AssistantService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_listen)
            .setContentTitle(if (wake) "Listening for “${app.settings.wakeWordLabel}”" else "${app.settings.assistantName} is connected")
            .setContentText("Tap to open StellAI")
            .setContentIntent(open)
            .addAction(0, if (wake) "Stop listening" else "Disconnect", stop)
            .setOngoing(true)
            .setSilent(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    companion object {
        private const val TAG = "AssistantService"
        private const val CHANNEL_ID = "wakeword"
        private const val NOTIFICATION_ID = 1
        private const val ACTION_STOP = "com.stellai.app.STOP_LISTENING"

        /** Call only while an activity is visible. Safe to call again: it re-checks what's needed. */
        fun start(context: Context) {
            try {
                ContextCompat.startForegroundService(context, Intent(context, AssistantService::class.java))
            } catch (e: Exception) {
                Log.e(TAG, "start failed", e)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, AssistantService::class.java))
        }
    }
}
