package com.kenigevents.mymediabank;

import java.net.URI;
import java.util.Locale;

/** Adapted from Projects Hub's exact-origin native capability boundary. */
final class WebOriginPolicy {
    private final String host;
    private final int port;

    WebOriginPolicy(String baseUrl) {
        URI base = parse(baseUrl);
        if (base == null || !"https".equalsIgnoreCase(base.getScheme())
                || base.getHost() == null || base.getUserInfo() != null
                || base.getQuery() != null || base.getFragment() != null
                || effectivePort(base) < 1 || effectivePort(base) > 65535) {
            throw new IllegalArgumentException("Backend must be an HTTPS URL without credentials");
        }
        host = base.getHost().toLowerCase(Locale.ROOT);
        port = effectivePort(base);
    }

    String allowedOrigin() {
        return "https://" + host + (port == 443 ? "" : ":" + port);
    }

    boolean isTrustedPage(String url) {
        URI value = parse(url);
        return value != null && value.getUserInfo() == null && sameOrigin(value);
    }

    boolean isTrustedPermissionOrigin(String url) {
        URI value = parse(url);
        if (value == null || value.getUserInfo() != null
                || value.getQuery() != null || value.getFragment() != null) return false;
        String path = value.getPath();
        return (path == null || path.isEmpty() || "/".equals(path)) && sameOrigin(value);
    }

    private boolean sameOrigin(URI value) {
        return "https".equalsIgnoreCase(value.getScheme())
                && value.getHost() != null && host.equalsIgnoreCase(value.getHost())
                && port == effectivePort(value);
    }

    private static int effectivePort(URI value) {
        if (value.getPort() >= 0) return value.getPort();
        return "https".equalsIgnoreCase(value.getScheme()) ? 443 : -1;
    }

    private static URI parse(String value) {
        try {
            return value == null ? null : URI.create(value);
        } catch (IllegalArgumentException invalid) {
            return null;
        }
    }
}
