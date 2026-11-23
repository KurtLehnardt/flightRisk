package com.flightrisk.app.drone

/**
 * What the screens say about obstacle avoidance. Store builds carry no depth
 * model (`midas_small.onnx` is not made by `scripts/mobile_models/make_models.py`),
 * so search-pattern flights do not check for obstacles; the state is shown
 * instead of being silently off.
 */
object ObstacleAvoidanceText {
    const val OFF_NO_MODEL = "Obstacle avoidance: off (no depth model)"
    const val OFF_NOT_LOADED = "Obstacle avoidance: off (the depth model did not load)"
    const val ON = "Obstacle avoidance: on"

    /** The line for a connected drone: [on] from `DroneManager.obstacleAvoidanceOn`, [modelInstalled] from the assets. */
    fun forDrone(on: Boolean?, modelInstalled: Boolean): String = when {
        !modelInstalled -> OFF_NO_MODEL
        on == true -> ON
        on == false -> OFF_NOT_LOADED
        else -> "Obstacle avoidance: checking"
    }

    /** The line for Settings, with or without a drone: on only when the model is installed. */
    fun forSettings(modelInstalled: Boolean): String =
        if (modelInstalled) "Obstacle avoidance: on when a search pattern is flown" else OFF_NO_MODEL
}
