package com.flightrisk.app.privacy

import java.io.File
import javax.xml.parsers.DocumentBuilderFactory
import org.w3c.dom.Element

class MemoryPrivacyPrefs(
    override var noticeAcknowledged: Boolean = false,
    override var recordSearchVideo: Boolean = false,
) : PrivacyPrefs

/** A (domain, path) pair of an exclude rule. */
data class ExcludeRule(val domain: String, val path: String)

object XmlRules {
    /** Unit tests run with the module directory (mobile/app) as working directory. */
    fun file(relative: String): File = File("src/main/$relative").also {
        check(it.isFile) { "missing ${it.absolutePath}" }
    }

    fun excludes(file: File, sectionTag: String? = null): List<ExcludeRule> {
        val doc = DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(file)
        val roots = if (sectionTag == null) {
            listOf(doc.documentElement)
        } else {
            val nodes = doc.getElementsByTagName(sectionTag)
            check(nodes.length == 1) { "expected one <$sectionTag> in ${file.name}" }
            listOf(nodes.item(0) as Element)
        }
        return roots.flatMap { root ->
            val list = root.getElementsByTagName("exclude")
            (0 until list.length).map {
                val e = list.item(it) as Element
                ExcludeRule(e.getAttribute("domain"), e.getAttribute("path"))
            }
        }
    }
}
