// 阿里云 ARTC 的 Python 薄封装：建引擎、入会、拿远端视频原始帧。
//
// 为什么不是 pybind11：这个项目的 Python 是 3.14，pybind11 在它下面装不上，
// 所以直接用 Python 的 C API，少一个构建依赖，也绕开版本兼容。
//
// 往外只有一个单例（SDK 本身也是单例），接口就五个函数 + 一个回调。
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <atomic>
#include <mutex>
#include <vector>

#include "rtc/engine_interface.h"
#include "rtc/engine_media_engine.h"

using namespace AliRTCSdk;

namespace {

std::mutex g_lock;
AliEngine* g_engine = nullptr;
IAliEngineMediaEngine* g_media = nullptr;
PyObject* g_video_cb = nullptr;  // 视频帧回调，跨线程使用要拿 GIL
PyObject* g_audio_cb = nullptr;  // 远端音频回调（查 Vidu 到底推不推声音）
std::atomic<long long> g_frames{0};
std::atomic<long long> g_audio_frames{0};
std::atomic<bool> g_alive{true};   // 销毁期间必须停止回调，否则 SDK 线程等 GIL、主线程等 SDK → 死锁

// 只关心远端画面：采集/编码前的回调直接放弃（返回 false = 不写回 SDK）。
class FrameObserver : public IVideoFrameObserver {
 public:
  bool OnCaptureVideoSample(AliEngineVideoSource, AliEngineVideoRawData&) override {
    return false;
  }
  bool OnPreEncodeVideoSample(AliEngineVideoSource, AliEngineVideoRawData&) override {
    return false;
  }
  // 编码后的数据（NALU）回调不是纯虚的，不用管；我们只要裸帧。

  AliEngineVideoFormat GetVideoFormatPreference() override { return AliEngineVideoFormatI420; }

  bool OnRemoteVideoSample(const char* uid,
                           AliEngineVideoSource,
                           AliEngineVideoRawData& raw) override {
    g_frames.fetch_add(1);
    if (!g_alive.load() || g_video_cb == nullptr || raw.dataYPtr == nullptr || raw.width <= 0 || raw.height <= 0) {
      return false;
    }
    PyGILState_STATE gil = PyGILState_Ensure();
    // I420：亮度 w×h，色度各至少 h/2 行；stride 可能比宽大，所以把整块带 stride 一起给出去
    const Py_ssize_t ny = static_cast<Py_ssize_t>(raw.strideY) * raw.height;
    const Py_ssize_t nu = static_cast<Py_ssize_t>(raw.strideU) * ((raw.height + 1) / 2);
    const Py_ssize_t nv = static_cast<Py_ssize_t>(raw.strideV) * ((raw.height + 1) / 2);
    PyObject* args = PyTuple_New(9);
    if (args != nullptr) {
      PyTuple_SET_ITEM(args, 0, PyUnicode_FromString(uid ? uid : ""));
      PyTuple_SET_ITEM(args, 1, PyLong_FromLong(raw.width));
      PyTuple_SET_ITEM(args, 2, PyLong_FromLong(raw.height));
      PyTuple_SET_ITEM(args, 3, PyBytes_FromStringAndSize(static_cast<const char*>(raw.dataYPtr), ny));
      PyTuple_SET_ITEM(args, 4, PyBytes_FromStringAndSize(static_cast<const char*>(raw.dataUPtr), nu));
      PyTuple_SET_ITEM(args, 5, PyBytes_FromStringAndSize(static_cast<const char*>(raw.dataVPtr), nv));
      PyTuple_SET_ITEM(args, 6, PyLong_FromLong(raw.strideY));
      PyTuple_SET_ITEM(args, 7, PyLong_FromLong(raw.strideU));
      PyTuple_SET_ITEM(args, 8, PyLong_FromLong(raw.strideV));
      PyObject* result = PyObject_CallObject(g_video_cb, args);
      if (result == nullptr) {
        PyErr_Print();  // 回调里的异常要打出来，不然只看到「没帧」
      }
      Py_XDECREF(result);
      Py_DECREF(args);
    }
    PyGILState_Release(gil);
    return false;  // 我们只读，不回写
  }
};

FrameObserver g_observer;

// 远端声音：只用来确认「Vidu 的流里到底有没有音频」，默认不开订阅。
class AudioObserver : public IAudioFrameObserver {
 public:
  bool OnCapturedAudioFrame(AliEngineAudioRawData) override { return false; }
  bool OnProcessCapturedAudioFrame(AliEngineAudioRawData) override { return false; }
  bool OnPublishAudioFrame(AliEngineAudioRawData) override { return false; }
  bool OnPlaybackAudioFrame(AliEngineAudioRawData) override { return false; }
  bool OnMixedAllAudioFrame(AliEngineAudioRawData) override { return false; }

  bool OnRemoteUserAudioFrame(const char* uid, AliEngineAudioRawData raw) override {
    g_audio_frames.fetch_add(1);
    if (!g_alive.load() || g_audio_cb == nullptr || raw.dataPtr == nullptr) {
      return false;
    }
    const Py_ssize_t n =
        static_cast<Py_ssize_t>(raw.numOfSamples) * raw.numOfChannels * raw.bytesPerSample;
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject* args = PyTuple_New(8);
    if (args != nullptr) {
      PyTuple_SET_ITEM(args, 0, PyUnicode_FromString(uid ? uid : ""));
      PyTuple_SET_ITEM(args, 1, PyLong_FromLong(raw.samplesPerSec));
      PyTuple_SET_ITEM(args, 2, PyLong_FromLong(raw.numOfChannels));
      PyTuple_SET_ITEM(args, 3, PyLong_FromLong(raw.bytesPerSample));
      PyTuple_SET_ITEM(args, 4, PyLong_FromLong(raw.numOfSamples));
      PyTuple_SET_ITEM(args, 5, PyBytes_FromStringAndSize(static_cast<const char*>(raw.dataPtr), n));
      PyTuple_SET_ITEM(args, 6, PyBool_FromLong(raw.sentenceBegin));
      PyTuple_SET_ITEM(args, 7, PyBool_FromLong(raw.sentenceEnd));
      PyObject* result = PyObject_CallObject(g_audio_cb, args);
      if (result == nullptr) {
        PyErr_Print();
      }
      Py_XDECREF(result);
      Py_DECREF(args);
    }
    PyGILState_Release(gil);
    return false;
  }
};

AudioObserver g_audio_observer;

// 这里只用来把「入会成功/失败」这种关键事件带出来，其余事件不管。
class EventListener : public AliEngineEventListener {
 public:
  void OnJoinChannelResult(int result,
                           const char* channel,
                           const char* userId,
                           int elapsed) override {
    if (g_video_cb != nullptr) {  // 复用同一个回调对象，按事件名区分太重，这里只打日志
      return;
    }
    printf("[alirtc] join result=%d channel=%s user=%s elapsed=%d\n", result, channel, userId,
           elapsed);
    fflush(stdout);
  }
};

EventListener g_listener;

PyObject* py_create(PyObject*, PyObject* args) {
  const char* extras = "";
  if (!PyArg_ParseTuple(args, "|s", &extras)) {
    return nullptr;
  }
  std::lock_guard<std::mutex> guard(g_lock);
  if (g_engine != nullptr) {
    Py_RETURN_TRUE;
  }
  g_alive.store(true);
  g_engine = AliEngine::Create(extras);
  if (g_engine == nullptr) {
    Py_RETURN_FALSE;
  }
  g_engine->SetEngineEventListener(&g_listener);
  void* media = nullptr;
  if (g_engine->QueryInterface(AliEngineInterfaceMediaEngine, &media) == 0 && media != nullptr) {
    g_media = static_cast<IAliEngineMediaEngine*>(media);
    g_media->RegisterVideoSampleObserver(&g_observer);
    g_media->RegisterAudioFrameObserver(&g_audio_observer);
  }
  Py_RETURN_TRUE;
}

PyObject* py_set_video_callback(PyObject*, PyObject* cb) {
  if (cb == Py_None) {
    Py_XDECREF(g_video_cb);
    g_video_cb = nullptr;
    Py_RETURN_NONE;
  }
  if (!PyCallable_Check(cb)) {
    PyErr_SetString(PyExc_TypeError, "回调必须是可调用的");
    return nullptr;
  }
  Py_INCREF(cb);
  Py_XDECREF(g_video_cb);
  g_video_cb = cb;
  Py_RETURN_NONE;
}

PyObject* py_set_audio_callback(PyObject*, PyObject* cb) {
  if (cb == Py_None) {
    Py_XDECREF(g_audio_cb);
    g_audio_cb = nullptr;
    Py_RETURN_NONE;
  }
  if (!PyCallable_Check(cb)) {
    PyErr_SetString(PyExc_TypeError, "回调必须是可调用的");
    return nullptr;
  }
  Py_INCREF(cb);
  Py_XDECREF(g_audio_cb);
  g_audio_cb = cb;
  Py_RETURN_NONE;
}

// 订阅远端音频（默认关）：开会话前调，之后再入会。
PyObject* py_subscribe_audio(PyObject*, PyObject* arg) {
  int on = PyObject_IsTrue(arg);
  if (g_engine == nullptr) {
    PyErr_SetString(PyExc_RuntimeError, "先 create 再设订阅");
    return nullptr;
  }
  g_engine->SetDefaultSubscribeAllRemoteAudioStreams(on != 0);
  if (g_media != nullptr && on) {
    AliEngineAudioFrameObserverConfig cfg;
    g_media->EnableAudioFrameObserver(true, AliEngineAudioSourceRemoteUser, cfg);
  }
  return PyBool_FromLong(on != 0);
}

PyObject* py_audio_count(PyObject*, PyObject*) { return PyLong_FromLongLong(g_audio_frames.load()); }

PyObject* py_join(PyObject*, PyObject* args) {
  const char *token, *channel, *user, *name = "";
  if (!PyArg_ParseTuple(args, "sss|s", &token, &channel, &user, &name)) {
    return nullptr;
  }
  if (g_engine == nullptr) {
    PyErr_SetString(PyExc_RuntimeError, "先 create 再 join");
    return nullptr;
  }
  // 默认还是只要画面；要听远端声音就走 subscribe_audio(True)（实验/调试用）
  if (g_audio_cb == nullptr) {
    g_engine->SetDefaultSubscribeAllRemoteAudioStreams(false);
    g_engine->SubscribeAllRemoteAudioStreams(false);
  }
  g_engine->SetDefaultSubscribeAllRemoteVideoStreams(true);
  g_engine->PublishLocalVideoStream(false);
  g_engine->PublishLocalAudioStream(false);
  // 声音就交给 SDK 自己放：远端音频和画面是同一个 RTC 流、同一个抖动缓冲，
  // 同步由 SDK 保证。我们只在本地播就会差好几秒（实测云端音频回声比画面早 0.6~几秒）。
  return PyLong_FromLong(g_engine->JoinChannel(token, channel, user, name));
}

PyObject* py_leave(PyObject*, PyObject*) {
  if (g_engine == nullptr) {
    return PyLong_FromLong(-1);
  }
  int code = -1;
  Py_BEGIN_ALLOW_THREADS  // 离会会等线程收尾，不能占着 GIL 等
  code = g_engine->LeaveChannel();
  Py_END_ALLOW_THREADS
  return PyLong_FromLong(code);
}

PyObject* py_destroy(PyObject*, PyObject*) {
  std::lock_guard<std::mutex> guard(g_lock);
  g_alive.store(false);  // 先断路，再拆
  if (g_media != nullptr) {
    g_media->UnRegisterVideoSampleObserver(&g_observer);
    g_media = nullptr;
  }
  if (g_engine != nullptr) {
    Py_BEGIN_ALLOW_THREADS  // 同上：Destroy 会 join 内部线程
    AliEngine::Destroy();
    Py_END_ALLOW_THREADS
    g_engine = nullptr;
  }
  Py_XDECREF(g_video_cb);
  g_video_cb = nullptr;
  Py_RETURN_NONE;
}

PyObject* py_frame_count(PyObject*, PyObject*) { return PyLong_FromLongLong(g_frames.load()); }

PyMethodDef kMethods[] = {
    {"create", py_create, METH_VARARGS, "创建引擎（单例），参数：extras"},
    {"set_video_callback", py_set_video_callback, METH_O, "设远端视频帧回调"},
    {"join", py_join, METH_VARARGS, "入会：token, channel, user, name"},
    {"leave", py_leave, METH_NOARGS, "离会"},
    {"destroy", py_destroy, METH_NOARGS, "销毁引擎"},
    {"frame_count", py_frame_count, METH_NOARGS, "收到的帧数（判断有没有画面）"},
    {"set_audio_callback", py_set_audio_callback, METH_O, "设远端音频回调"},
    {"subscribe_audio", py_subscribe_audio, METH_O, "订阅/取消远端音频（入会前设）"},
    {"audio_count", py_audio_count, METH_NOARGS, "收到的远端音频帧数"},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef kModule = {PyModuleDef_HEAD_INIT, "_alirtc", "阿里云 ARTC 的薄封装", -1, kMethods,
                       nullptr, nullptr, nullptr, nullptr};

}  // namespace

PyMODINIT_FUNC PyInit__alirtc(void) { return PyModule_Create(&kModule); }
