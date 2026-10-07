package com.kenigevents.mymediabank;

import org.junit.Test;
import static org.junit.Assert.*;

public class WebOriginPolicyTest {
    private final WebOriginPolicy origin = new WebOriginPolicy("https://my-mediabank.kenigevents.ru/");

    @Test public void allowsOnlyTheExactHttpsOrigin() {
        assertTrue(origin.isTrustedPage("https://my-mediabank.kenigevents.ru/view?tab=1"));
        assertTrue(origin.isTrustedPermissionOrigin("https://my-mediabank.kenigevents.ru:443/"));
        for (String url : new String[]{null, "", "https://my-mediabank.kenigevents.ru.evil.test/",
                "https://evil.my-mediabank.kenigevents.ru/", "http://my-mediabank.kenigevents.ru/",
                "https://my-mediabank.kenigevents.ru:444/", "https://owner@my-mediabank.kenigevents.ru/",
                "https://my-mediabank.kenigevents.ru@evil.test/", "file:///etc/passwd", "data:text/html,test",
                "javascript:alert(1)", "//my-mediabank.kenigevents.ru/", " https://my-mediabank.kenigevents.ru/"}) {
            assertFalse(url, origin.isTrustedPage(url));
            assertFalse(url, origin.isTrustedPermissionOrigin(url));
        }
    }

    @Test public void permissionOriginsCannotContainPathQueryOrFragment() {
        assertFalse(origin.isTrustedPermissionOrigin("https://my-mediabank.kenigevents.ru/photo"));
        assertFalse(origin.isTrustedPermissionOrigin("https://my-mediabank.kenigevents.ru/?token=test"));
        assertFalse(origin.isTrustedPermissionOrigin("https://my-mediabank.kenigevents.ru/#photo"));
    }

    @Test public void configurableTlsPortIsStillAnExactBoundary() {
        WebOriginPolicy custom = new WebOriginPolicy("https://example.test:8443/app/");
        assertEquals("https://example.test:8443", custom.allowedOrigin());
        assertTrue(custom.isTrustedPage("https://example.test:8443/app/"));
        assertFalse(custom.isTrustedPage("https://example.test/"));
    }

    @Test public void rejectsUnsafeBackendConfiguration() {
        for (String url : new String[]{"http://example.test", "file:///tmp/index.html",
                "https://user:password@example.test/", "https://example.test/?token=secret",
                "https://example.test/#x", "https://example.test:0/", "https://example.test:65536/"}) {
            try { new WebOriginPolicy(url); fail(url); } catch (IllegalArgumentException expected) { }
        }
    }
}
