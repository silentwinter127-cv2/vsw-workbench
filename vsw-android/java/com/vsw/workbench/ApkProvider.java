package com.vsw.workbench;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;

/**
 * 只读地把「下载好的安装包」交给系统安装器。
 *
 * 为什么必须有这个类：Android 7 以后禁止把 `file://` 交给别的应用（FileUriExposedException），
 * 安装器只认 `content://`。androidx 的 FileProvider 会拖进一整套依赖，而整个工程是手工
 * javac + d8 编译的、刻意不带 androidx，所以这里自己实现一个最小 Provider：
 * 只认 `content://com.vsw.workbench.files/apk/<文件名>`，只读，文件限定在 cache/update 里。
 */
public class ApkProvider extends ContentProvider {

    public static final String AUTHORITY = "com.vsw.workbench.files";
    /** 安装包放在这里（应用私有缓存目录，退出后可能被系统清理 —— 需要重下就是）。 */
    public static final String SUBDIR = "update";

    /** 把 cache/update 下的文件名变成对外可见的 content URI。 */
    public static Uri uriFor(String fileName) {
        return Uri.parse("content://" + AUTHORITY + "/apk/" + fileName);
    }

    private File resolve(Uri uri) throws FileNotFoundException {
        String name = uri == null ? null : uri.getLastPathSegment();
        if (name == null || name.isEmpty()) {
            throw new FileNotFoundException("没有给文件名");
        }
        // 只允许一层文件名：挡掉 ../ 之类想跳出目录的路径
        if (name.contains("/") || name.contains("\\") || name.contains("..")) {
            throw new FileNotFoundException("文件名不合法：" + name);
        }
        File dir = new File(getContext().getCacheDir(), SUBDIR);
        File file = new File(dir, name);
        if (!file.isFile()) {
            throw new FileNotFoundException("文件不存在：" + file.getAbsolutePath());
        }
        return file;
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        // 只读：安装器只会读，写没有任何正当用途
        return ParcelFileDescriptor.open(resolve(uri), ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        try {
            File file = resolve(uri);
            String[] cols = projection != null && projection.length > 0
                    ? projection : new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE};
            MatrixCursor cursor = new MatrixCursor(cols, 1);
            Object[] row = new Object[cols.length];
            for (int i = 0; i < cols.length; i++) {
                if (OpenableColumns.SIZE.equals(cols[i])) {
                    row[i] = file.length();
                } else {
                    row[i] = file.getName();
                }
            }
            cursor.addRow(row);
            return cursor;
        } catch (FileNotFoundException exc) {
            return null;
        }
    }

    @Override
    public String getType(Uri uri) {
        return "application/vnd.android.package-archive";
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        return 0;
    }
}
