package com.echogemini.terminal

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Starts the terminal at boot.
 *
 * Once [MainActivity] is set as the HOME activity, Android launches it at boot
 * anyway and this receiver is redundant. It is kept because during bring-up the
 * app is often installed alongside the stock LineageOS launcher and has not yet
 * been made home -- and "the clock comes back after a reboot" is verification
 * step 12's second half. A redundant receiver is cheaper than a device that
 * shows a blank launcher after a power cut.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_LOCKED_BOOT_COMPLETED -> {
                AlarmScheduler.get(context).restore()
                if (intent.action == Intent.ACTION_LOCKED_BOOT_COMPLETED) return
                Log.i(TAG, "boot completed; starting the terminal")
                val launch = Intent(context, MainActivity::class.java).apply {
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                }
                try {
                    context.startActivity(launch)
                } catch (e: Exception) {
                    Log.e(TAG, "could not start MainActivity at boot", e)
                }
            }
        }
    }

    private companion object {
        const val TAG = "EchoBoot"
    }
}
