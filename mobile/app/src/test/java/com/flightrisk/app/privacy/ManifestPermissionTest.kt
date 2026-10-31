package com.flightrisk.app.privacy

import java.io.File
import javax.xml.parsers.DocumentBuilderFactory
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element

class ManifestPermissionTest {
    private fun permissions(): Set<String> {
        val doc = DocumentBuilderFactory.newInstance().newDocumentBuilder()
            .parse(XmlRules.file("AndroidManifest.xml"))
        val list = doc.getElementsByTagName("uses-permission")
        return (0 until list.length).map {
            (list.item(it) as Element).getAttribute("android:name")
        }.toSet()
    }

    @Test fun changeWifiStateIsNotRequested() {
        assertFalse(permissions().contains("android.permission.CHANGE_WIFI_STATE"))
    }

    @Test fun codeNeverChangesWifiState() {
        val banned = listOf("setWifiEnabled(", "enableNetwork(", "addNetwork(", "WifiNetworkSuggestion", "WifiManager.reconnect")
        File("src/main/java").walkTopDown().filter { it.extension == "kt" }.forEach { f ->
            val text = f.readText()
            for (b in banned) assertFalse("${f.name} uses $b", text.contains(b))
        }
    }

    @Test fun neededPermissionsRemain() {
        val p = permissions()
        for (n in listOf("CAMERA", "ACCESS_FINE_LOCATION", "INTERNET", "ACCESS_WIFI_STATE", "ACCESS_NETWORK_STATE")) {
            assertTrue(n, p.contains("android.permission.$n"))
        }
    }
}
