package com.kenigevents.mymediabank;

import java.util.Comparator;

/** Only lightweight metadata is held for the chronological browsing snapshot. */
final class PhotoRecord {
    static final Comparator<PhotoRecord> NEWEST_FIRST =
            Comparator.comparingLong((PhotoRecord photo) -> photo.takenAt).reversed()
                    .thenComparing(Comparator.comparingLong((PhotoRecord photo) -> photo.id).reversed());

    final long id;
    final String name;
    final long takenAt;

    PhotoRecord(long id, String name, long dateTakenMs, long dateAddedSeconds) {
        this.id = id;
        this.name = name == null ? "Фото " + id : name;
        this.takenAt = dateTakenMs > 0 ? dateTakenMs : Math.max(0L, dateAddedSeconds) * 1000L;
    }
}
