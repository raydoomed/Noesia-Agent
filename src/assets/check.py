"""素材体检：图/视频的真实属性。

为什么要有这个模块：目录名和扩展名都会说谎（`透明/` 里可能全是 RGB，棋盘格是画进像素里的
预览背景；`final` 可能是半成品）。所以一律用 PIL 读 mode/alpha、用 ffprobe 读编码/帧率，
并且把这些检查变成可重复运行的命令（`noesia check-assets`），而不是一次性的人工检查。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from settings import AvatarSection, Resolver as PathResolver

ALPHA_MODES = {"RGBA", "LA", "PA"}
ERROR = "error"
WARN = "warn"
INFO = "info"


@dataclass
class Finding:
    level: str
    target: str
    message: str


@dataclass
class ImageInfo:
    path: str
    mode: str
    width: int
    height: int
    has_alpha: bool = False
    alpha_fully_opaque: bool = False
    corner_kind: str = ""
    corner_rgb: tuple[int, int, int] | None = None


@dataclass
class VideoInfo:
    path: str
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration_s: float = 0.0
    has_alpha: bool = False


@dataclass
class AssetReport:
    images: list[ImageInfo] = field(default_factory=list)
    videos: list[VideoInfo] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(f.level == ERROR for f in self.findings)


def _display_path(path: Path, root: Path) -> str:
    """报告里显示的路径：优先相对项目根。

    素材目录允许配绝对路径（放外置盘）：那时 `relative_to` 会抛 ValueError，
    整个 `check-assets` 直接崩掉——而这是唯一能发现素材问题的命令，不能因为显示方式就不可用。
    """
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _is_gray(rgb: tuple[int, int, int], tolerance: int = 10) -> bool:
    r, g, b = rgb
    return max(abs(r - g), abs(g - b), abs(r - b)) <= tolerance


def _classify_corner(image: Image.Image) -> tuple[str, tuple[int, int, int] | None]:
    """看四个角，判断背景是什么。

    返回 (类型, 角点颜色)。类型：transparent / green（绿幕）/ checkerboard（棋盘格已烘焙）/ solid / mixed
    """
    if image.mode in ALPHA_MODES:
        alpha = image.convert("RGBA").getchannel("A")
        if alpha.getextrema()[0] == 0:
            return "transparent", None

    rgb = image.convert("RGB")
    width, height = rgb.size
    probe = 12
    boxes = [
        (0, 0, probe, probe),
        (width - probe, 0, width, probe),
        (0, height - probe, probe, height),
        (width - probe, height - probe, width, height),
    ]
    colors: list[tuple[int, int, int]] = []
    for box in boxes:
        for item in rgb.crop(box).getcolors(maxcolors=4096) or []:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            _count, color = item
            if not isinstance(color, tuple) or len(color) != 3:
                continue
            colors.append((int(color[0]), int(color[1]), int(color[2])))

    if not colors:
        return "mixed", None

    mean = (
        sum(c[0] for c in colors) // len(colors),
        sum(c[1] for c in colors) // len(colors),
        sum(c[2] for c in colors) // len(colors),
    )
    r, g, b = mean
    if g > 150 and g - r > 60 and g - b > 60:
        return "green", mean

    grays = {c for c in colors if _is_gray(c)}
    if len(grays) >= 2 and len(colors) >= 2:
        spread = max(max(c) for c in grays) - min(min(c) for c in grays)
        if spread >= 6:
            return "checkerboard", mean
    if len(set(colors)) <= 2:
        return "solid", mean
    return "mixed", mean


def inspect_image(path: Path, display: str | None = None) -> tuple[ImageInfo, list[Finding]]:
    label = display or str(path)
    findings: list[Finding] = []
    with Image.open(path) as image:
        image.load()
        info = ImageInfo(
            path=label,
            mode=image.mode,
            width=image.width,
            height=image.height,
            has_alpha=image.mode in ALPHA_MODES,
        )
        if info.has_alpha:
            alpha = image.convert("RGBA").getchannel("A")
            low, _high = alpha.getextrema()
            info.alpha_fully_opaque = low == 255
            if info.alpha_fully_opaque:
                findings.append(Finding(WARN, label, "有 alpha 通道但全不透明（等于没有透明区域）"))
        corner_kind, corner_rgb = _classify_corner(image)
        info.corner_kind = corner_kind
        info.corner_rgb = corner_rgb

    if not info.has_alpha:
        if corner_kind == "checkerboard":
            findings.append(Finding(
                ERROR, label,
                "没有 alpha 通道，背景是已烘焙的棋盘格像素（导出时把透明预览存成了图）——不能当 mask 用，需从源头重新导出 PNG-32",
            ))
        elif corner_kind == "green":
            findings.append(Finding(
                WARN, label,
                "没有 alpha 通道，背景是绿幕——使用前需抠像为 alpha，注意绿边与头发丝",
            ))
        else:
            findings.append(Finding(ERROR, label, f"没有 alpha 通道（mode={info.mode}），无法直接叠加到桌面"))
    return info, findings


def inspect_video(path: Path, display: str | None = None) -> tuple[VideoInfo, list[Finding]]:
    label = display or str(path)
    findings: list[Finding] = []
    info = VideoInfo(path=label)
    if not shutil.which("ffprobe"):
        findings.append(Finding(WARN, label, "未找到 ffprobe，跳过视频体检"))
        return info, findings

    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if proc.returncode != 0:
        findings.append(Finding(ERROR, label, f"ffprobe 读取失败：{proc.stderr.strip()[:200]}"))
        return info, findings

    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        findings.append(Finding(ERROR, label, "没有视频流"))
        return info, findings

    info.codec = video_stream.get("codec_name", "")
    info.width = int(video_stream.get("width") or 0)
    info.height = int(video_stream.get("height") or 0)
    info.has_alpha = "alpha_mode" in video_stream or video_stream.get("pix_fmt", "").endswith("a")
    rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0/1"
    try:
        numerator, denominator = rate.split("/")
        info.fps = float(numerator) / float(denominator) if float(denominator) else 0.0
    except (ValueError, ZeroDivisionError):
        info.fps = 0.0
    try:
        info.duration_s = float(data.get("format", {}).get("duration") or 0.0)
    except ValueError:
        info.duration_s = 0.0

    if not info.has_alpha:
        findings.append(Finding(
            WARN, label,
            f"视频无 alpha 通道（pix_fmt={video_stream.get('pix_fmt')}）——待机态要透明悬浮，靠 Qt 侧实时抠像",
        ))
    return info, findings


def inspect_assets(resolver: PathResolver, avatar: AvatarSection) -> AssetReport:
    report = AssetReport()
    avatar_dir = resolver.resolve(avatar.dir)

    if not avatar_dir.is_dir():
        report.findings.append(Finding(ERROR, avatar.dir, "形象目录不存在"))
        return report

    image_paths = [avatar_dir / avatar.reference.front]

    sizes: dict[tuple[int, int], list[str]] = {}
    for path in image_paths:
        display = _display_path(path, resolver.root)
        if not path.is_file():
            report.findings.append(Finding(ERROR, display, "配置里引用的形象图不存在"))
            continue
        info, findings = inspect_image(path, display)
        report.images.append(info)
        report.findings.extend(findings)
        sizes.setdefault((info.width, info.height), []).append(display)

    idle_path = resolver.resolve(avatar.idle_video)
    if not idle_path.is_file():
        report.findings.append(Finding(ERROR, avatar.idle_video, "待机视频不存在"))
    else:
        display = _display_path(idle_path, resolver.root)
        video, findings = inspect_video(idle_path, display)
        report.videos.append(video)
        report.findings.extend(findings)
        image_ratios = {(w / h) for (w, h) in sizes if h}
        if video.height and image_ratios:
            ratio = video.width / video.height
            if all(abs(ratio - r) > 0.02 for r in image_ratios):
                report.findings.append(Finding(
                    WARN, display,
                    f"宽高比 {ratio:.3f} 与形象图不一致——待机视频里的人物和静态图会大小不一",
                ))
    return report


def render_report(report: AssetReport) -> str:
    lines: list[str] = ["== 图 =="]
    for info in report.images:
        alpha = "alpha" if info.has_alpha else "无alpha"
        extra = "全不透明" if info.alpha_fully_opaque else ""
        corner = info.corner_kind
        lines.append(f"  {info.path}  {info.mode} {info.width}x{info.height} {alpha}{extra} 背景={corner}")
    lines.append("== 视频 ==")
    for video in report.videos:
        lines.append(
            f"  {video.path}  {video.codec} {video.width}x{video.height} @{video.fps:.2f}fps "
            f"{video.duration_s:.2f}s alpha={'是' if video.has_alpha else '否'}"
        )
    errors = [f for f in report.findings if f.level == ERROR]
    warns = [f for f in report.findings if f.level == WARN]
    infos = [f for f in report.findings if f.level == INFO]
    lines.append(f"== 结论：{len(errors)} error / {len(warns)} warn / {len(infos)} info ==")
    for finding in errors + warns + infos:
        lines.append(f"  [{finding.level.upper()}] {finding.target}: {finding.message}")
    return "\n".join(lines)
