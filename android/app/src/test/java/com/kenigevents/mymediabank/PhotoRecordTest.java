package com.kenigevents.mymediabank;

import org.junit.Test;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import static org.junit.Assert.*;

public class PhotoRecordTest {
    @Test public void ordersRecentScreenshotsWithMissingExifByAddedDate() {
        PhotoRecord older = new PhotoRecord(1, "camera.jpg", 10000, 1);
        PhotoRecord screenshot = new PhotoRecord(2, "screenshot.png", 0, 20);
        List<PhotoRecord> photos = new ArrayList<>(Arrays.asList(older, screenshot));
        photos.sort(PhotoRecord.NEWEST_FIRST);
        assertEquals(2, photos.get(0).id);
        assertEquals(20000, screenshot.takenAt);
    }

    @Test public void equalTimestampsHaveAStableDescendingIdTieBreak() {
        List<PhotoRecord> photos = new ArrayList<>(Arrays.asList(
                new PhotoRecord(5, "a", 12345, 3), new PhotoRecord(8, "b", 12345, 2),
                new PhotoRecord(1, "newer", 12346, 1)));
        photos.sort(PhotoRecord.NEWEST_FIRST);
        assertEquals(1, photos.get(0).id);
        assertEquals(8, photos.get(1).id);
        assertEquals(5, photos.get(2).id);
    }
}
