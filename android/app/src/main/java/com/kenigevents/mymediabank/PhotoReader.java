package com.kenigevents.mymediabank;

import android.content.ContentResolver;
import android.content.ContentUris;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.ImageDecoder;
import android.graphics.Matrix;
import android.media.ExifInterface;
import android.net.Uri;
import android.os.Build;
import android.provider.MediaStore;
import android.util.Base64;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;

/** Read-only MediaStore access. All enumeration, decoding and encoding runs on one worker. */
final class PhotoReader {
    static final int MAX_SIDE = 2048;
    static final int MAX_JPEG_BYTES = 2 * 1024 * 1024;
    private final ContentResolver resolver;
    private final AtomicLong refreshVersion = new AtomicLong();
    private long snapshotVersion = -1;
    private List<PhotoRecord> snapshot = new ArrayList<>();

    PhotoReader(ContentResolver resolver) {
        this.resolver = resolver;
    }

    void refresh() {
        refreshVersion.incrementAndGet();
    }

    JSONObject read(int index) throws IOException, JSONException {
        long version = refreshVersion.get();
        if (snapshotVersion != version) {
            snapshot = enumerate();
            snapshotVersion = version;
        }
        if (snapshot.isEmpty()) throw new PhotoException("no_photos", "Нет доступных фотографий. Выберите снимки или разрешите доступ в настройках.");
        if (index < 0 || index >= snapshot.size()) throw new PhotoException("end_of_photos", "Это последняя доступная фотография.");
        PhotoRecord photo = snapshot.get(index);
        Uri uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, photo.id);
        Bitmap bitmap = decode(uri);
        try {
            byte[] jpeg;
            while (true) {
                ByteArrayOutputStream output = new ByteArrayOutputStream(256 * 1024);
                if (!bitmap.compress(Bitmap.CompressFormat.JPEG, 85, output)) throw new IOException("JPEG encode failed");
                jpeg = output.toByteArray();
                if (jpeg.length <= MAX_JPEG_BYTES) break;
                int width = Math.max(1, (int) (bitmap.getWidth() * 0.75));
                int height = Math.max(1, (int) (bitmap.getHeight() * 0.75));
                if (width == bitmap.getWidth() && height == bitmap.getHeight()) throw new IOException("Image cannot fit upload bound");
                Bitmap smaller = Bitmap.createScaledBitmap(bitmap, width, height, true);
                bitmap.recycle();
                bitmap = smaller;
            }
            return new JSONObject()
                    .put("id", Long.toString(photo.id))
                    .put("index", index)
                    .put("total", snapshot.size())
                    .put("name", photo.name)
                    .put("takenAt", photo.takenAt)
                    .put("width", bitmap.getWidth())
                    .put("height", bitmap.getHeight())
                    .put("dataUrl", "data:image/jpeg;base64," + Base64.encodeToString(jpeg, Base64.NO_WRAP));
        } finally {
            bitmap.recycle();
        }
    }

    private List<PhotoRecord> enumerate() throws IOException {
        List<PhotoRecord> result = new ArrayList<>();
        String[] columns = {MediaStore.Images.Media._ID, MediaStore.Images.Media.DISPLAY_NAME,
                MediaStore.Images.Media.DATE_TAKEN, MediaStore.Images.Media.DATE_ADDED};
        String selection = null;
        if (Build.VERSION.SDK_INT >= 30) selection = MediaStore.MediaColumns.IS_PENDING + "=0 AND " + MediaStore.MediaColumns.IS_TRASHED + "=0";
        else if (Build.VERSION.SDK_INT >= 29) selection = MediaStore.MediaColumns.IS_PENDING + "=0";
        try (Cursor cursor = resolver.query(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, columns, selection, null, null)) {
            if (cursor == null) throw new IOException("MediaStore is unavailable");
            while (cursor.moveToNext()) {
                result.add(new PhotoRecord(cursor.getLong(0), cursor.getString(1), cursor.getLong(2), cursor.getLong(3)));
            }
        }
        result.sort(PhotoRecord.NEWEST_FIRST);
        return result;
    }

    private Bitmap decode(Uri uri) throws IOException {
        if (Build.VERSION.SDK_INT >= 28) {
            // ImageDecoder applies EXIF orientation and supports device HEIC codecs.
            return ImageDecoder.decodeBitmap(ImageDecoder.createSource(resolver, uri), (decoder, info, source) -> {
                decoder.setAllocator(ImageDecoder.ALLOCATOR_SOFTWARE);
                int width = info.getSize().getWidth();
                int height = info.getSize().getHeight();
                double scale = Math.min(1.0, MAX_SIDE / (double) Math.max(width, height));
                decoder.setTargetSize(Math.max(1, (int) (width * scale)), Math.max(1, (int) (height * scale)));
            });
        }
        BitmapFactory.Options options = new BitmapFactory.Options();
        options.inJustDecodeBounds = true;
        try (InputStream stream = requireStream(uri)) {
            BitmapFactory.decodeStream(stream, null, options);
        }
        if (options.outWidth <= 0 || options.outHeight <= 0) throw new IOException("Unsupported image format");
        options.inSampleSize = 1;
        while (Math.max(options.outWidth, options.outHeight) / options.inSampleSize > MAX_SIDE) options.inSampleSize *= 2;
        options.inJustDecodeBounds = false;
        options.inPreferredConfig = Bitmap.Config.ARGB_8888;
        Bitmap bitmap;
        try (InputStream stream = requireStream(uri)) {
            bitmap = BitmapFactory.decodeStream(stream, null, options);
        }
        if (bitmap == null) throw new IOException("Photo cannot be decoded");
        // Some codecs round sampled dimensions up; retain the hard pixel bound.
        if (Math.max(bitmap.getWidth(), bitmap.getHeight()) > MAX_SIDE) {
            double scale = MAX_SIDE / (double) Math.max(bitmap.getWidth(), bitmap.getHeight());
            try {
                Bitmap bounded = Bitmap.createScaledBitmap(bitmap, Math.max(1, (int) (bitmap.getWidth() * scale)),
                        Math.max(1, (int) (bitmap.getHeight() * scale)), true);
                if (bounded != bitmap) bitmap.recycle();
                bitmap = bounded;
            } catch (RuntimeException | OutOfMemoryError failed) {
                bitmap.recycle();
                throw failed;
            }
        }
        int orientation = ExifInterface.ORIENTATION_NORMAL;
        try (InputStream stream = requireStream(uri)) {
            orientation = new ExifInterface(stream).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL);
        } catch (IOException ignored) {
            // Images without EXIF remain valid; no original is changed.
        }
        Matrix transform = new Matrix();
        switch (orientation) {
            case ExifInterface.ORIENTATION_FLIP_HORIZONTAL: transform.setScale(-1, 1); break;
            case ExifInterface.ORIENTATION_ROTATE_180: transform.setRotate(180); break;
            case ExifInterface.ORIENTATION_FLIP_VERTICAL: transform.setScale(1, -1); break;
            case ExifInterface.ORIENTATION_TRANSPOSE: transform.setRotate(90); transform.postScale(-1, 1); break;
            case ExifInterface.ORIENTATION_ROTATE_90: transform.setRotate(90); break;
            case ExifInterface.ORIENTATION_TRANSVERSE: transform.setRotate(-90); transform.postScale(-1, 1); break;
            case ExifInterface.ORIENTATION_ROTATE_270: transform.setRotate(-90); break;
            default: return bitmap;
        }
        try {
            Bitmap oriented = Bitmap.createBitmap(bitmap, 0, 0, bitmap.getWidth(), bitmap.getHeight(), transform, true);
            if (oriented != bitmap) bitmap.recycle();
            return oriented;
        } catch (RuntimeException | OutOfMemoryError failed) {
            bitmap.recycle();
            throw failed;
        }
    }

    private InputStream requireStream(Uri uri) throws IOException {
        InputStream stream = resolver.openInputStream(uri);
        if (stream == null) throw new IOException("Photo is unavailable");
        return stream;
    }

    static final class PhotoException extends IOException {
        final String code;
        PhotoException(String code, String message) {
            super(message);
            this.code = code;
        }
    }
}
