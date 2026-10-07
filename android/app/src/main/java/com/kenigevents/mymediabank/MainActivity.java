package com.kenigevents.mymediabank;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.PermissionRequest;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.util.Collections;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/** A thin photo source around the shared browser Live client. No native voice transport. */
public final class MainActivity extends Activity {
    private static final int REQUEST_PHOTOS = 100;
    private static final int REQUEST_MICROPHONE = 101;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final AtomicLong photoGeneration = new AtomicLong();
    // At most the active decode and one latest request; no gallery bitmap cache.
    private final ThreadPoolExecutor photoWorker = new ThreadPoolExecutor(1, 1, 0L,
            TimeUnit.MILLISECONDS, new ArrayBlockingQueue<>(1), new ThreadPoolExecutor.DiscardOldestPolicy());
    private FrameLayout root;
    private LinearLayout fallback;
    private TextView fallbackText;
    private ProgressBar loading;
    private Button retry;
    private WebView webView;
    private WebOriginPolicy origin;
    private PhotoReader photos;
    private boolean foreground;
    private boolean browserPaused;
    private boolean pageFailed;
    private boolean bridgeAvailable;
    private boolean photoPermissionWanted;
    private boolean pendingPhotoAfterPermission;
    private int requestedPhoto;
    private int runtimePermissionInFlight;
    private PermissionRequest pendingWebPermission;
    private final Runnable loadTimeout = () -> showFailure("Сервер долго не отвечает. Проверьте подключение и повторите.");

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(Color.BLACK);
        getWindow().setNavigationBarColor(Color.BLACK);
        if (Build.VERSION.SDK_INT >= 30) getWindow().setDecorFitsSystemWindows(false);
        else getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
        buildFallback();
        try {
            origin = new WebOriginPolicy(BuildConfig.BACKEND_URL);
            photos = new PhotoReader(getContentResolver());
            webView = new WebView(this);
            root.addView(webView, 0, new FrameLayout.LayoutParams(-1, -1));
            configureWebView();
            if (bridgeAvailable) loadBackend();
        } catch (RuntimeException unavailable) {
            showFailure("Не удалось открыть Android WebView. Обновите Android System WebView и повторите.");
        }
    }

    private void buildFallback() {
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            } else {
                view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                        insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            }
            return insets;
        });
        fallback = new LinearLayout(this);
        fallback.setOrientation(LinearLayout.VERTICAL);
        fallback.setGravity(Gravity.CENTER);
        fallback.setPadding(dp(28), dp(28), dp(28), dp(28));
        fallback.setBackgroundColor(Color.BLACK);
        TextView title = new TextView(this);
        title.setText("My MediaBank");
        title.setTextColor(Color.WHITE);
        title.setTextSize(24);
        fallback.addView(title);
        loading = new ProgressBar(this);
        LinearLayout.LayoutParams spinner = new LinearLayout.LayoutParams(dp(36), dp(36));
        spinner.topMargin = dp(24);
        fallback.addView(loading, spinner);
        fallbackText = new TextView(this);
        fallbackText.setTextColor(Color.LTGRAY);
        fallbackText.setTextSize(16);
        fallbackText.setGravity(Gravity.CENTER);
        fallbackText.setPadding(0, dp(20), 0, dp(20));
        fallback.addView(fallbackText);
        retry = new Button(this);
        retry.setText("Повторить");
        retry.setAllCaps(false);
        retry.setOnClickListener(view -> {
            if (webView == null || !bridgeAvailable) recreate();
            else loadBackend();
        });
        fallback.addView(retry);
        root.addView(fallback, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);
        showLoading();
    }

    private void configureWebView() {
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        webView.setBackgroundColor(Color.BLACK);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setSafeBrowsingEnabled(true);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setUserAgentString(settings.getUserAgentString() + " MyMediaBankAndroid/" + BuildConfig.VERSION_NAME);
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, false);

        // Same scoped message API as Projects Hub. Never add a JavaScript interface to every frame.
        bridgeAvailable = WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER);
        if (!bridgeAvailable) {
            showFailure("Обновите Android System WebView: установленная версия не поддерживает безопасный доступ к фотографиям.");
            return;
        }
        WebViewCompat.addWebMessageListener(webView, "MediaBankAndroid", Collections.singleton(origin.allowedOrigin()),
                (source, message, sourceOrigin, isMainFrame, reply) -> {
                    if (!isMainFrame || !origin.isTrustedPermissionOrigin(sourceOrigin.toString())
                            || !origin.isTrustedPage(source.getUrl()) || !foreground) return;
                    try {
                        String raw = message.getData();
                        if (raw == null || raw.length() > 512) return;
                        JSONObject input = new JSONObject(raw);
                        switch (input.getString("action")) {
                            case "photo":
                                Object index = input.get("index");
                                if (input.length() != 2 || !(index instanceof Integer) || (Integer) index < 0) return;
                                requestPhoto((Integer) index);
                                break;
                            case "permissions":
                                if (input.length() != 1) return;
                                requestedPhoto = 0;
                                pendingPhotoAfterPermission = true;
                                photoPermissionWanted = true;
                                drainPermissions();
                                break;
                            case "app_info":
                                if (input.length() == 1) emit("onAppInfo", appInfo());
                                break;
                            case "settings":
                                if (input.length() == 1) openSettings();
                                break;
                            default: emitError("invalid_action", "Неизвестное действие приложения.");
                        }
                    } catch (JSONException invalid) {
                        emitError("invalid_request", "Не удалось прочитать запрос приложения.");
                    }
                });

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                if (origin.isTrustedPage(request.getUrl().toString())) return false;
                if (request.isForMainFrame()) openExternal(request.getUrl());
                return true;
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap icon) {
                photoGeneration.incrementAndGet();
                denyPendingMicrophone();
                pageFailed = false;
                if (!origin.isTrustedPage(url)) {
                    pageFailed = true;
                    view.stopLoading();
                    showFailure("Не удалось открыть сервер приложения. Повторите подключение.");
                    return;
                }
                showLoading();
                main.removeCallbacks(loadTimeout);
                main.postDelayed(loadTimeout, 20000);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (!origin.isTrustedPage(url) || pageFailed) return;
                main.removeCallbacks(loadTimeout);
                fallback.setVisibility(View.GONE);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (!request.isForMainFrame()) return;
                pageFailed = true;
                showFailure("Не удалось подключиться к серверу. Проверьте интернет и повторите.");
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (!request.isForMainFrame()) return;
                pageFailed = true;
                showFailure("Сервер временно недоступен (" + response.getStatusCode() + "). Повторите подключение.");
            }

            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.cancel();
                pageFailed = true;
                showFailure("Не удалось проверить защищённое соединение с сервером. Проверьте дату телефона и повторите.");
            }

            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                photoGeneration.incrementAndGet();
                denyPendingMicrophone();
                root.removeView(view);
                view.destroy();
                if (webView == view) webView = null;
                showFailure("Android WebView остановился. Нажмите «Повторить», чтобы открыть приложение снова.");
                return true;
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                main.post(() -> {
                    if (!trustedMicRequest(request) || !foreground) { request.deny(); return; }
                    boolean audio = false;
                    for (String resource : request.getResources()) {
                        if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) audio = true;
                    }
                    if (!audio) { request.deny(); return; }
                    denyPendingMicrophone();
                    pendingWebPermission = request;
                    drainPermissions();
                });
            }

            @Override
            public void onPermissionRequestCanceled(PermissionRequest request) {
                main.post(() -> { if (pendingWebPermission == request) pendingWebPermission = null; });
            }
        });
    }

    private void requestPhoto(int index) {
        requestedPhoto = index;
        long generation = photoGeneration.incrementAndGet();
        if ("denied".equals(photoAccess())) {
            pendingPhotoAfterPermission = true;
            emitError("permission_required", "Разрешите доступ к фотографиям или выберите отдельные снимки.");
            if (!getPreferences(MODE_PRIVATE).getBoolean("photos_asked", false)) {
                photoPermissionWanted = true;
                drainPermissions();
            }
            return;
        }
        photoWorker.execute(() -> {
            if (generation != photoGeneration.get()) return;
            try {
                JSONObject photo = photos.read(index);
                main.post(() -> {
                    if (generation != photoGeneration.get() || !foreground || isDestroyed()) return;
                    String access = photoAccess();
                    if ("denied".equals(access)) {
                        photos.refresh();
                        emitError("permission_required", "Доступ к фотографиям изменился. Выберите снимки заново.");
                        return;
                    }
                    try { photo.put("access", access); } catch (JSONException ignored) { }
                    emit("onPhoto", photo);
                });
            } catch (PhotoReader.PhotoException expected) {
                photoError(generation, expected.code, expected.getMessage());
            } catch (SecurityException revoked) {
                photos.refresh();
                photoError(generation, "permission_required", "Доступ к фотографиям изменился. Выберите снимки заново.");
            } catch (IOException | JSONException | RuntimeException unavailable) {
                photoError(generation, "photo_unavailable", "Не удалось прочитать фотографию. Можно перейти к следующей.");
            } catch (OutOfMemoryError lowMemory) {
                photoError(generation, "low_memory", "Не хватило памяти для фотографии. Закройте лишние приложения или перейдите к следующей.");
            }
        });
    }

    private void photoError(long generation, String code, String message) {
        main.post(() -> {
            if (generation == photoGeneration.get() && foreground && !isDestroyed()) emitError(code, message);
        });
    }

    private String photoAccess() {
        String full = Build.VERSION.SDK_INT >= 33 ? Manifest.permission.READ_MEDIA_IMAGES : Manifest.permission.READ_EXTERNAL_STORAGE;
        if (checkSelfPermission(full) == PackageManager.PERMISSION_GRANTED) return "full";
        if (Build.VERSION.SDK_INT >= 34 && checkSelfPermission(Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED)
                == PackageManager.PERMISSION_GRANTED) return "selected";
        return "denied";
    }

    private JSONObject appInfo() {
        JSONObject info = new JSONObject();
        try {
            info.put("versionName", BuildConfig.VERSION_NAME).put("versionCode", BuildConfig.VERSION_CODE)
                    .put("photoAccess", photoAccess())
                    .put("microphone", checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED ? "granted" : "denied");
        } catch (JSONException impossible) { }
        return info;
    }

    private void drainPermissions() {
        if (!foreground || runtimePermissionInFlight != 0 || isDestroyed()) return;
        if (pendingWebPermission != null) {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                PermissionRequest request = pendingWebPermission;
                pendingWebPermission = null;
                if (trustedMicRequest(request)) request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                else request.deny();
            } else if (getPreferences(MODE_PRIVATE).getBoolean("microphone_asked", false)
                    && !shouldShowRequestPermissionRationale(Manifest.permission.RECORD_AUDIO)) {
                denyPendingMicrophone();
                emitError("microphone_denied", "Разрешите микрофон в настройках приложения и включите Миру снова.");
            } else {
                getPreferences(MODE_PRIVATE).edit().putBoolean("microphone_asked", true).apply();
                runtimePermissionInFlight = REQUEST_MICROPHONE;
                requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQUEST_MICROPHONE);
                return;
            }
        }
        if (!photoPermissionWanted) { loadPendingPhoto(); return; }
        photoPermissionWanted = false;
        if ("full".equals(photoAccess())) { emit("onPermissions", appInfo()); loadPendingPhoto(); return; }
        String permission = Build.VERSION.SDK_INT >= 33 ? Manifest.permission.READ_MEDIA_IMAGES : Manifest.permission.READ_EXTERNAL_STORAGE;
        if ("denied".equals(photoAccess()) && getPreferences(MODE_PRIVATE).getBoolean("photos_asked", false)
                && !shouldShowRequestPermissionRationale(permission)) {
            new AlertDialog.Builder(this).setTitle("Доступ к фотографиям")
                    .setMessage("Разрешите доступ или выберите фотографии в настройках приложения.")
                    .setPositiveButton("Открыть настройки", (dialog, which) -> openSettings())
                    .setNegativeButton("Позже", null).show();
            return;
        }
        getPreferences(MODE_PRIVATE).edit().putBoolean("photos_asked", true).apply();
        runtimePermissionInFlight = REQUEST_PHOTOS;
        if (Build.VERSION.SDK_INT >= 34) {
            requestPermissions(new String[]{Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED}, REQUEST_PHOTOS);
        } else {
            requestPermissions(new String[]{permission}, REQUEST_PHOTOS);
        }
    }

    private void loadPendingPhoto() {
        if (pendingPhotoAfterPermission && !"denied".equals(photoAccess())) {
            pendingPhotoAfterPermission = false;
            requestPhoto(requestedPhoto);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode != REQUEST_PHOTOS && requestCode != REQUEST_MICROPHONE) return;
        runtimePermissionInFlight = 0;
        if (photos != null) photos.refresh();
        if (requestCode == REQUEST_MICROPHONE) {
            // If the permission overlay paused this Activity, leave an authorized
            // request pending until onResume. A real onStop already denied it.
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                denyPendingMicrophone();
                emitError("microphone_denied", "Без микрофона фотографии доступны. Разрешить микрофон можно в настройках приложения.");
            }
        } else if ("denied".equals(photoAccess())) {
            emitError("permission_denied", "Доступ к фотографиям не разрешён. Нажмите «Доступ к фото» или откройте настройки приложения.");
        }
        emit("onPermissions", appInfo());
        main.post(this::drainPermissions);
    }

    private boolean trustedMicRequest(PermissionRequest request) {
        return webView != null && request.getOrigin() != null && origin != null
                && origin.isTrustedPermissionOrigin(request.getOrigin().toString())
                && origin.isTrustedPage(webView.getUrl());
    }

    private void denyPendingMicrophone() {
        PermissionRequest pending = pendingWebPermission;
        pendingWebPermission = null;
        if (pending != null) pending.deny();
    }

    private void emit(String callback, JSONObject data) {
        if (webView == null || origin == null || isDestroyed() || !origin.isTrustedPage(webView.getUrl())) return;
        String json = data == null ? "" : data.toString().replace("\u2028", "\\u2028").replace("\u2029", "\\u2029");
        webView.evaluateJavascript("window.MyMediaBank && window.MyMediaBank." + callback
                + " && window.MyMediaBank." + callback + "(" + json + ")", null);
    }

    private void emitError(String code, String message) {
        try {
            emit("onNativeError", new JSONObject().put("code", code).put("message", message).put("index", requestedPhoto));
        } catch (JSONException impossible) { }
    }

    private void openSettings() {
        try {
            startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName())));
        } catch (ActivityNotFoundException unavailable) {
            emitError("settings_unavailable", "Откройте разрешения My MediaBank в настройках телефона.");
        }
    }

    private void openExternal(Uri uri) {
        if (!"https".equalsIgnoreCase(uri.getScheme()) && !"http".equalsIgnoreCase(uri.getScheme())) return;
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (ActivityNotFoundException unavailable) {
            showFailure("Не найден браузер для открытия ссылки. Нажмите «Повторить», чтобы вернуться.");
        }
    }

    private void loadBackend() {
        pageFailed = false;
        showLoading();
        webView.loadUrl(BuildConfig.BACKEND_URL);
    }

    private void showLoading() {
        fallback.setVisibility(View.VISIBLE);
        loading.setVisibility(View.VISIBLE);
        retry.setVisibility(View.GONE);
        fallbackText.setText("Подключаюсь к Мире…");
    }

    private void showFailure(String message) {
        main.removeCallbacks(loadTimeout);
        fallback.setVisibility(View.VISIBLE);
        loading.setVisibility(View.GONE);
        retry.setVisibility(View.VISIBLE);
        fallbackText.setText(message);
    }

    @Override
    protected void onResume() {
        super.onResume();
        foreground = true;
        boolean wasPaused = browserPaused;
        browserPaused = false;
        if (photos != null) photos.refresh();
        if (webView != null && wasPaused) webView.onResume();
        if (wasPaused) emit("onForeground", null);
        emit("onPermissions", appInfo());
        drainPermissions();
    }

    @Override
    protected void onPause() {
        foreground = false;
        // A runtime permission overlay can pause the Activity without leaving
        // the app. Keep the initial Live request until its permission resolves;
        // onStop still closes it if the user presses Home from that dialog.
        if (runtimePermissionInFlight == 0) pauseBrowser();
        super.onPause();
    }

    @Override
    protected void onStop() {
        pauseBrowser();
        super.onStop();
    }

    private void pauseBrowser() {
        if (browserPaused) return;
        browserPaused = true;
        photoGeneration.incrementAndGet();
        emit("onBackground", null);
        denyPendingMicrophone();
        if (webView != null) webView.onPause();
    }

    @Override
    protected void onDestroy() {
        photoGeneration.incrementAndGet();
        main.removeCallbacksAndMessages(null);
        denyPendingMicrophone();
        photoWorker.shutdownNow();
        if (webView != null) {
            root.removeView(webView);
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
