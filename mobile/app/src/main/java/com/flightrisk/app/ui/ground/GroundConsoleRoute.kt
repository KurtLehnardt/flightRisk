package com.flightrisk.app.ui.ground

import android.content.Context
import android.content.ContextWrapper
import androidx.activity.ComponentActivity
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.ViewModelProvider

/**
 * The navigation entry of the Ground console. The ViewModel belongs to the
 * activity (the same instance `MainActivity` drives from its lifecycle), so
 * the ground link survives switching tabs and the ViewModel is released only
 * when the activity is finished.
 */
@Composable
fun GroundConsoleRoute(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val viewModel = remember(context) {
        val activity = context.findComponentActivity()
        ViewModelProvider(activity, GroundConsoleViewModel.factory(activity))[GroundConsoleViewModel::class.java]
    }
    GroundConsoleScreen(viewModel, modifier)
}

private fun Context.findComponentActivity(): ComponentActivity {
    var current: Context = this
    while (current is ContextWrapper) {
        if (current is ComponentActivity) return current
        current = current.baseContext
    }
    error("The Ground console needs to be shown inside a ComponentActivity")
}
