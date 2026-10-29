package com.localai.remote

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
 * Keeps the app alive in the background so it can listen for the wake word.
 * Android requires a visible notification for this. It can only be started
 * while the app is on screen (Android 14+ rule for microphone services).
 */
class AssistantService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val app = application as App
        createChannel()
        try {
            ServiceCompat.startForeground(
                this, NOTIFICATION_ID, buildNotification(app),
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0,
            )
        } catch (e: Exception) {
            // Started from the background, or microphone permission missing.
            Log.e(TAG, "couldn't start in foreground", e)
            stopSelf()
            return
        }
        // Keep the CPU awake so audio keeps flowing with the screen off.
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "LaptopAI:wakeword")
            .apply { setReferenceCounted(false); acquire() }
        app.voice.setWakeMode(true)
        app.onAssistantServiceChanged(true)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            (application as App).settings.wakeWordEnabled = false
            stopSelf()
        }
        // Not sticky: Android won't allow restarting a microphone service from the background anyway.
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        val app = application as App
        app.voice.setWakeMode(false)
        app.onAssistantServiceChanged(false)
        wakeLock?.let { if (it.isHeld) it.release() }
        super.onDestroy()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(CHANNEL_ID, "Wake word listening", NotificationManager.IMPORTANCE_LOW)
            channel.setShowBadge(false)
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }

    private fun buildNotification(app: App): Notification {
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
            .setContentTitle("Listening for “${app.settings.wakeWordLabel}”")
            .setContentText("Tap to open Laptop AI")
            .setContentIntent(open)
            .addAction(0, "Stop listening", stop)
            .setOngoing(true)
            .setSilent(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    companion object {
        private const val TAG = "AssistantService"
        private const val CHANNEL_ID = "wakeword"
        private const val NOTIFICATION_ID = 1
        private const val ACTION_STOP = "com.localai.remote.STOP_LISTENING"

        /** Call only while an activity is visible. */
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
