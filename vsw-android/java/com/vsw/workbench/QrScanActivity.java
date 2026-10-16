package com.vsw.workbench;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.hardware.Camera;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.view.Surface;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import com.google.zxing.BinaryBitmap;
import com.google.zxing.DecodeHintType;
import com.google.zxing.LuminanceSource;
import com.google.zxing.MultiFormatReader;
import com.google.zxing.Result;
import com.google.zxing.common.HybridBinarizer;

import java.util.EnumMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 原生扫码页：只用 ZXing core（一个纯 Java JAR，无第三方依赖）解 iPhone 式的二维码。
 *
 * 为什么值得单独写一个页面：工作台地址是 http://192.168.x.x:8799 这种东西，
 * 让普通用户在手机上敲一遍几乎不可能出错率极低 —— 对着电脑屏幕扫一下是唯一可用的交互。
 *
 * 相机用平台自带的 android.hardware.Camera（API 1 起就有），不引 CameraX / Play Services，
 * 所以这个 APK 的依赖只有 ZXing 一个。解码在后台线程做，靠 takePicture 的 JPEG 回调拿帧
 * （比 onPreviewFrame 的 YUV 更好写，而且静态二维码对帧率没要求）。
 */
public class QrScanActivity extends Activity implements SurfaceHolder.Callback, Camera.PictureCallback {

    public static final String EXTRA_RESULT = "result";

    private static final String TAG = "vsw-qr";
    private static final long FRAME_INTERVAL_MS = 260;   // 约 4 帧/秒，静态目标够用且不烫手

    private SurfaceView surface;
    private TextView hint;
    private Camera camera;
    private Camera.Size previewSize;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final MultiFormatReader reader = new MultiFormatReader();
    private final AtomicBoolean decoding = new AtomicBoolean(false);
    private boolean finished;
    private long lastFrameAt;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        surface = new SurfaceView(this);
        root.addView(surface, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        surface.getHolder().addCallback(this);

        LinearLayout overlay = new LinearLayout(this);
        overlay.setOrientation(LinearLayout.VERTICAL);
        overlay.setGravity(Gravity.CENTER_HORIZONTAL);

        hint = new TextView(this);
        hint.setText("把电脑屏幕上工作台启动日志里的二维码放进画面");
        hint.setTextColor(Color.WHITE);
        hint.setTextSize(15f);
        hint.setGravity(Gravity.CENTER);
        hint.setPadding(28, 40, 28, 0);
        overlay.addView(hint);

        TextView tip = new TextView(this);
        tip.setText("扫不到？启动日志里也有地址，可以直接手填");
        tip.setTextColor(0xB0FFFFFF);
        tip.setTextSize(12.5f);
        tip.setGravity(Gravity.CENTER);
        tip.setPadding(28, 14, 28, 0);
        overlay.addView(tip);

        TextView cancel = new TextView(this);
        cancel.setText("取消");
        cancel.setTextColor(0xFF4EA1FF);
        cancel.setTextSize(15f);
        cancel.setGravity(Gravity.CENTER);
        cancel.setPadding(28, 34, 28, 28);
        cancel.setOnClickListener(new android.view.View.OnClickListener() {
            @Override
            public void onClick(android.view.View v) {
                finishWith(null);
            }
        });
        overlay.addView(cancel);

        root.addView(overlay, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        setContentView(root);

        Map<DecodeHintType, Object> hints = new EnumMap<>(DecodeHintType.class);
        hints.put(DecodeHintType.TRY_HARDER, Boolean.TRUE);
        // 只用 QR：目标就是我们自己生成的码，收窄格式能让解码快一截
        hints.put(DecodeHintType.POSSIBLE_FORMATS,
                java.util.Collections.singletonList(com.google.zxing.BarcodeFormat.QR_CODE));
        reader.setHints(hints);
    }

    // ------------------------------------------------------------------ 相机
    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        if (!openCamera(holder)) {
            toast("打不开相机，请检查相机权限");
            finishWith(null);
        }
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
        if (camera == null) return;
        try {
            camera.stopPreview();
            camera.setPreviewDisplay(holder);
            camera.startPreview();
            scheduleFrame();
        } catch (Exception exc) {
            Log.w(TAG, "surfaceChanged: " + exc.getMessage());
        }
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        releaseCamera();
    }

    private boolean openCamera(SurfaceHolder holder) {
        try {
            Camera.CameraInfo info = new Camera.CameraInfo();
            int count = Camera.getNumberOfCameras();
            int chosen = -1;
            for (int i = 0; i < count; i++) {
                Camera.getCameraInfo(i, info);
                if (info.facing == Camera.CameraInfo.CAMERA_FACING_BACK) {
                    chosen = i;
                    break;
                }
            }
            camera = chosen >= 0 ? Camera.open(chosen) : Camera.open();
            if (camera == null) return false;

            Camera.Parameters params = camera.getParameters();
            previewSize = pickPreviewSize(params.getSupportedPreviewSizes());
            if (previewSize != null) {
                params.setPreviewSize(previewSize.width, previewSize.height);
            }
            // 连续对焦对近距离拍屏幕最有用；不支持就退回自动对焦
            List<String> focusModes = params.getSupportedFocusModes();
            if (focusModes != null) {
                if (focusModes.contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE)) {
                    params.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE);
                } else if (focusModes.contains(Camera.Parameters.FOCUS_MODE_AUTO)) {
                    params.setFocusMode(Camera.Parameters.FOCUS_MODE_AUTO);
                }
            }
            params.setPictureFormat(android.graphics.ImageFormat.JPEG);
            camera.setParameters(params);
            camera.setDisplayOrientation(displayRotation());
            camera.setPreviewDisplay(holder);
            camera.startPreview();
            scheduleFrame();
            return true;
        } catch (Exception exc) {
            Log.w(TAG, "openCamera failed: " + exc.getMessage());
            releaseCamera();
            return false;
        }
    }

    /** 挑一个不太大的分辨率：足够解二维码，又能让每帧解码控制在几十毫秒内。 */
    private Camera.Size pickPreviewSize(List<Camera.Size> sizes) {
        if (sizes == null || sizes.isEmpty()) return null;
        Camera.Size best = null;
        for (Camera.Size size : sizes) {
            long pixels = (long) size.width * size.height;
            if (pixels > 1920L * 1080L) continue;              // 太大没必要
            if (best == null || pixels > (long) best.width * best.height) {
                best = size;
            }
        }
        return best != null ? best : sizes.get(0);
    }

    private int displayRotation() {
        int rotation = getWindowManager().getDefaultDisplay().getRotation();
        int degrees = rotation == Surface.ROTATION_90 ? 90
                : rotation == Surface.ROTATION_180 ? 180
                : rotation == Surface.ROTATION_270 ? 270 : 0;
        Camera.CameraInfo info = new Camera.CameraInfo();
        try {
            Camera.getCameraInfo(0, info);
        } catch (Exception exc) {
            return degrees;
        }
        if (info.facing == Camera.CameraInfo.CAMERA_FACING_FRONT) {
            return (360 - (info.orientation + degrees) % 360) % 360;
        }
        return (info.orientation - degrees + 360) % 360;
    }

    private final Runnable frameTask = new Runnable() {
        @Override
        public void run() {
            grabFrame();
        }
    };

    private void scheduleFrame() {
        main.postDelayed(frameTask, FRAME_INTERVAL_MS);
    }

    private void grabFrame() {
        if (finished || camera == null) return;
        long now = System.currentTimeMillis();
        if (now - lastFrameAt < FRAME_INTERVAL_MS || decoding.get()) {
            scheduleFrame();
            return;
        }
        lastFrameAt = now;
        try {
            camera.takePicture(null, null, this);
        } catch (Exception exc) {
            Log.w(TAG, "takePicture: " + exc.getMessage());
            scheduleFrame();
        }
    }

    @Override
    public void onPictureTaken(byte[] data, Camera camera) {
        if (finished) return;
        decoding.set(true);
        final Camera cameraRef = camera;
        final byte[] frame = data;
        new Thread(new Runnable() {
            @Override
            public void run() {
                String text = null;
                try {
                    text = decode(frame);
                } catch (Exception exc) {
                    Log.w(TAG, "decode: " + exc.getMessage());
                }
                final String found = text;
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        decoding.set(false);
                        if (found != null) {
                            finishWith(found);
                        } else if (!finished) {
                            try {
                                cameraRef.startPreview();
                            } catch (Exception exc) {
                                Log.w(TAG, "restart preview: " + exc.getMessage());
                            }
                            scheduleFrame();
                        }
                    }
                });
            }
        }, "vsw-qr-decode").start();
    }

    /** 用 ZXing 解一帧 JPEG。正面和反色各试一次（屏幕反光或深色码都能兜住）。 */
    private String decode(byte[] jpeg) {
        android.graphics.Bitmap bitmap = android.graphics.BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length);
        if (bitmap == null) return null;
        int width = bitmap.getWidth();
        int height = bitmap.getHeight();
        int[] pixels = new int[width * height];
        bitmap.getPixels(pixels, 0, width, 0, 0, width, height);
        bitmap.recycle();

        // 自己转灰度：RGBLuminanceSource 只看红通道，屏幕照片的偏色会拖累识别
        byte[] gray = new byte[width * height];
        for (int i = 0; i < pixels.length; i++) {
            int color = pixels[i];
            int r = (color >> 16) & 0xFF;
            int g = (color >> 8) & 0xFF;
            int b = color & 0xFF;
            gray[i] = (byte) ((r * 77 + g * 151 + b * 28) >> 8);
        }
        String text = tryDecode(new GraySource(gray, width, height));
        if (text != null) return text;
        // 反色再试一次：屏幕上深底浅码、或者拍照时反光严重的情况都能兜住
        try {
            return tryDecode(new GraySource(gray, width, height).invert());
        } catch (Exception exc) {
            return null;
        }
    }

    private String tryDecode(LuminanceSource source) {
        try {
            Result result = reader.decodeWithState(new BinaryBitmap(new HybridBinarizer(source)));
            return result == null ? null : result.getText();
        } catch (Exception exc) {
            return null;   // NotFoundException 是常态：这一帧里没有码
        } finally {
            reader.reset();
        }
    }

    /** 极简灰度源：避开 ZXing 的 RGB 通道约定问题。 */
    private static final class GraySource extends LuminanceSource {
        private final byte[] data;

        GraySource(byte[] data, int width, int height) {
            super(width, height);
            this.data = data;
        }

        @Override
        public byte[] getRow(int y, byte[] row) {
            int width = getWidth();
            if (row == null || row.length < width) row = new byte[width];
            System.arraycopy(data, y * width, row, 0, width);
            return row;
        }

        @Override
        public byte[] getMatrix() {
            return data;
        }
    }

    private void finishWith(String text) {
        if (finished) return;
        finished = true;
        main.removeCallbacksAndMessages(null);
        releaseCamera();
        Intent data = new Intent();
        data.putExtra(EXTRA_RESULT, text);
        setResult(text != null ? RESULT_OK : RESULT_CANCELED, data);
        finish();
    }

    private void releaseCamera() {
        if (camera != null) {
            try {
                camera.stopPreview();
            } catch (Exception ignored) {
                // 预览已经不在了
            }
            try {
                camera.release();
            } catch (Exception ignored) {
                // 忽略
            }
            camera = null;
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        main.removeCallbacksAndMessages(null);
        releaseCamera();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (surface != null && surface.getHolder().getSurface() != null && camera == null && !finished) {
            openCamera(surface.getHolder());
        }
    }

    private void toast(String text) {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
    }
}
