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
import java.util.List;

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
    /**
     * 递给网页的那张待搜图片也走同一个 Provider。
     *
     * 网页的 `<input type=file>` 只认 `content://`：我们给它的必须是别的应用/渲染进程能通过
     * ContentResolver 读到的东西，不能是 `file://`。同一套「只读 + 只允许一层文件名」的规矩
     * 复用一次，不用再写第二个 Provider（多一个就要多注册一个 authority）。
     */
    public static final String SHARE = "websearch";

    /** 把 cache/update 下的文件名变成对外可见的 content URI。 */
    public static Uri uriFor(String fileName) {
        return uriFor(SUBDIR, fileName);
    }

    /** 把 cache/<sub>/<文件名> 变成 content URI。 */
    public static Uri uriFor(String sub, String fileName) {
        return Uri.parse("content://" + AUTHORITY + "/" + sub + "/" + fileName);
    }

    private File resolve(Uri uri) throws FileNotFoundException {
        List<String> parts = uri == null ? null : uri.getPathSegments();
        String sub = SUBDIR;
        String name;
        if (parts != null && parts.size() >= 2) {
            sub = parts.get(0);
            name = parts.get(1);
        } else {
            name = uri == null ? null : uri.getLastPathSegment();
        }
        if (name == null || name.isEmpty()) {
            throw new FileNotFoundException("没有给文件名");
        }
        // 只允许一层文件名：挡掉 ../ 之类想跳出目录的路径
        if (name.contains("/") || name.contains("\\") || name.contains("..")) {
            throw new FileNotFoundException("文件名不合法：" + name);
        }
        if (sub.contains("/") || sub.contains("\\") || sub.contains("..")) {
            throw new FileNotFoundException("目录不合法：" + sub);
        }
        File dir = new File(getContext().getCacheDir(), sub);
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
        List<String> parts = uri == null ? null : uri.getPathSegments();
        if (parts != null && parts.size() >= 2 && SHARE.equals(parts.get(0))) {
            String name = parts.get(1).toLowerCase(java.util.Locale.US);
            if (name.endsWith(".png")) return "image/png";
            if (name.endsWith(".webp")) return "image/webp";
            return "image/jpeg";
        }
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
