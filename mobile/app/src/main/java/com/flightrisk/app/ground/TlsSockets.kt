package com.flightrisk.app.ground

import java.io.IOException
import java.net.Socket
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLPeerUnverifiedException
import javax.net.ssl.SSLSocket
import javax.net.ssl.SSLSocketFactory

/**
 * How a `wss://` or `https://` connection is secured: the platform's own [SSLSocketFactory] (the default
 * one trusts only the system certificate store, as the app's network security config says), and a
 * check that the certificate was issued for the host name the user typed.
 *
 * There is no way in this app to trust an unknown certificate, to skip the host name check or to pin a
 * different store. [socketFactory] and [hostnameVerifier] exist so a test can trust its own test-only
 * certificate; production code leaves both at their defaults.
 *
 * @param host the name or IP literal the certificate must have been issued for; also sent as the SNI name.
 */
class TlsParams(
    val host: String,
    val socketFactory: SSLSocketFactory = defaultSocketFactory(),
    val hostnameVerifier: HostnameVerifier? = platformHostnameVerifier(),
) {
    companion object {
        /** The platform default: system trust store, platform cipher suites. */
        fun defaultSocketFactory(): SSLSocketFactory = SSLSocketFactory.getDefault() as SSLSocketFactory

        /**
         * On Android, the platform verifier (`HttpsURLConnection.getDefaultHostnameVerifier()`) is a real
         * check, run in addition to the endpoint identification the handshake already does. On a desktop
         * JVM that default verifier refuses everything, so there only the handshake's check applies.
         */
        fun platformHostnameVerifier(): HostnameVerifier? {
            val vm = System.getProperty("java.vm.name").orEmpty()
            val onAndroid = vm.contains("Dalvik", ignoreCase = true) || vm.equals("ART", ignoreCase = true) ||
                System.getProperty("java.vendor").orEmpty().contains("Android", ignoreCase = true)
            return if (onAndroid) HttpsURLConnection.getDefaultHostnameVerifier() else null
        }
    }
}

internal object TlsSockets {
    /**
     * Starts TLS over [plain], which is already connected. Only TLS 1.2 and 1.3 are enabled, the handshake
     * verifies the certificate chain against the platform trust store and the host name ("HTTPS" endpoint
     * identification), and the session is checked once more with the hostname verifier when there is one.
     * The handshake runs under [soTimeoutMs] so a peer that stalls cannot hold it forever.
     *
     * @throws IOException (an `SSLException`) when the handshake or the certificate check fails; [plain] is
     *   left open for the caller to close.
     */
    fun wrap(plain: Socket, params: TlsParams, port: Int, soTimeoutMs: Int): SSLSocket {
        val ssl = params.socketFactory.createSocket(plain, params.host, port, true) as SSLSocket
        try {
            val protocols = ssl.supportedProtocols.filter { it == "TLSv1.3" || it == "TLSv1.2" }
            if (protocols.isNotEmpty()) ssl.enabledProtocols = protocols.toTypedArray()
            val parameters = ssl.sslParameters
            parameters.endpointIdentificationAlgorithm = "HTTPS"
            ssl.sslParameters = parameters
            plain.soTimeout = soTimeoutMs.coerceAtLeast(1)
            ssl.startHandshake()
            val verifier = params.hostnameVerifier
            if (verifier != null && !verifier.verify(params.host, ssl.session)) {
                throw SSLPeerUnverifiedException("the certificate was not issued for the ground station's host name")
            }
            return ssl
        } catch (e: Throwable) {
            try {
                ssl.close()
            } catch (_: IOException) {
            }
            throw e
        }
    }
}
