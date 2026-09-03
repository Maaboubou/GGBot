"""图像/消息辅助工具。"""

from __future__ import annotations

from typing import Any


def is_uniform_column(image, x: int) -> bool:
    """判断截图中的某一列颜色是否基本一致。"""
    try:
        width, height = image.size
        if x < 0 or x >= width or height <= 0:
            return True
        colors = {image.getpixel((x, y)) for y in range(0, height, max(1, height // 40))}
        return len(colors) <= 3
    except Exception:
        return True


def find_content_center(control, threshold: int = 30) -> tuple[int, int] | None:
    """截图控件，寻找非背景内容的中心点。

    文件卡片等消息虽然 ListItem 覆盖整行，但实际可点击内容只占局部；
    通过背景色差异计算内容 bbox 后点击中心更可靠。
    """
    try:
        from PIL import ImageGrab
    except Exception:
        return None
    try:
        rect = control.BoundingRectangle
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None
        image = ImageGrab.grab(
            bbox=(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        ).convert("RGB")
        background = image.getpixel((width - 3, height - 3))
        xs = []
        ys = []
        for y in range(0, height, 2):
            for x in range(0, width, 2):
                pixel = image.getpixel((x, y))
                if (
                    abs(pixel[0] - background[0])
                    + abs(pixel[1] - background[1])
                    + abs(pixel[2] - background[2])
                ) > threshold:
                    xs.append(x)
                    ys.append(y)
        if not xs:
            return None
        return (
            int(rect.left) + (min(xs) + max(xs)) // 2,
            int(rect.top) + (min(ys) + max(ys)) // 2,
        )
    except Exception:
        return None


def _dominant_background_color(image) -> tuple[int, int, int]:
    """Return the dominant (quantized) RGB color of a UI screenshot."""
    width, height = image.size
    # Sampling keeps this helper cheap even when a ListItem spans a 4K window.
    step = max(1, int(((width * height) / 50000) ** 0.5))
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for y in range(0, height, step):
        for x in range(0, width, step):
            red, green, blue = image.getpixel((x, y))[:3]
            key = (red // 8, green // 8, blue // 8)
            values = buckets.setdefault(key, [0, 0, 0, 0])
            values[0] += 1
            values[1] += red
            values[2] += green
            values[3] += blue
    if not buckets:
        return 255, 255, 255
    count, red, green, blue = max(buckets.values(), key=lambda values: values[0])
    return red // count, green // count, blue // count


def quote_media_center_in_image(
    image,
    direction: str | None,
    threshold: int = 40,
) -> tuple[int, int] | None:
    """Locate the dense thumbnail inside the lower quote block of a message.

    A WeChat 4.x quote ``ListItem`` spans the entire chat width and has no UIA
    children.  Its scrollbar, avatar and reply bubble therefore make the usual
    whole-row content bounding box unsafe.  Quoted media is the only dense
    visual block in the lower half, so a small integral-image scan reliably
    finds it while constraining the scan to the sender's side of the row.

    The returned point is relative to ``image``.  ``None`` means no sufficiently
    dense block was found; callers may then use a direction-aware geometry
    fallback rather than clicking an arbitrary recent image.
    """
    try:
        image = image.convert("RGB")
        width, height = image.size
    except Exception:
        return None
    if width < 120 or height < 70:
        return None

    # The upper reply bubble can occupy roughly the first half of a compact
    # quote row.  Searching from 36% allowed that larger bubble to win over a
    # small quoted thumbnail (especially for white screenshots).  The quoted
    # payload is rendered in the bottom block, so exclude the upper 54%.
    lower = max(30, int(height * 0.54))
    upper = max(lower + 1, height - 7)
    side_margin = max(72, int(width * 0.055))
    if direction == "friend":
        left = side_margin
        right = min(int(width * 0.56), 720)
    elif direction == "self":
        left = max(int(width * 0.44), width - 720)
        right = width - side_margin
    else:
        # Unknown direction is still useful for pure visual detection, but keep
        # both edge strips out so avatars and the scrollbar cannot win.
        left = side_margin
        right = width - side_margin
    if right - left < 60 or upper - lower < 40:
        return None

    background = _dominant_background_color(image)
    sample_step = 2
    sample_xs = list(range(left, right, sample_step))
    sample_ys = list(range(lower, upper, sample_step))
    if not sample_xs or not sample_ys:
        return None

    # Integral image of a foreground-density score.  Dense photos/screenshots
    # score much higher than antialiased quote text or a one-pixel separator.
    integral: list[list[float]] = [[0.0] * (len(sample_xs) + 1)]
    bg_red, bg_green, bg_blue = background
    for y in sample_ys:
        previous = integral[-1]
        current = [0.0]
        row_sum = 0.0
        for index, x in enumerate(sample_xs, start=1):
            red, green, blue = image.getpixel((x, y))
            distance = (
                abs(red - bg_red)
                + abs(green - bg_green)
                + abs(blue - bg_blue)
            )
            weight = 0.0
            if distance > threshold:
                chroma = max(red, green, blue) - min(red, green, blue)
                weight = (
                    1.0
                    + min(distance, 300) / 300.0
                    + min(chroma, 128) / 256.0
                )
            row_sum += weight
            current.append(previous[index] + row_sum)
        integral.append(current)

    full_window = max(34, min(66, int(height * 0.21)))
    window = max(12, full_window // sample_step)
    window = min(window, len(sample_xs), len(sample_ys))
    if window < 12:
        return None

    def area_sum(x0: int, y0: int, x1: int, y1: int) -> float:
        return (
            integral[y1][x1]
            - integral[y0][x1]
            - integral[y1][x0]
            + integral[y0][x0]
        )

    max_x0 = len(sample_xs) - window
    max_y0 = len(sample_ys) - window
    x_positions = list(range(0, max_x0 + 1, 3))
    y_positions = list(range(0, max_y0 + 1, 3))
    if x_positions[-1] != max_x0:
        x_positions.append(max_x0)
    if y_positions[-1] != max_y0:
        y_positions.append(max_y0)

    best_score = 0.0
    best: tuple[int, int] | None = None
    area = float(window * window)
    for y0 in y_positions:
        y1 = y0 + window
        for x0 in x_positions:
            x1 = x0 + window
            score = area_sum(x0, y0, x1, y1) / area
            if score > best_score:
                best_score = score
                best = (x0, y0)

    if best is None or best_score < 0.24:
        return None
    x0, y0 = best
    return (
        left + (x0 * sample_step) + (window * sample_step) // 2,
        lower + (y0 * sample_step) + (window * sample_step) // 2,
    )


def find_quote_media_center(control, direction: str | None) -> tuple[int, int] | None:
    """Capture a quote row and return the quoted thumbnail's screen point."""
    try:
        from mabowx.core.win32 import capture_window_rect
    except Exception:
        return None
    try:
        rect = control.BoundingRectangle
        left = int(rect.left)
        top = int(rect.top)
        right = int(rect.right)
        bottom = int(rect.bottom)
        if right <= left or bottom <= top:
            return None
        top_control = control.GetTopLevelControl()
        hwnd = int(getattr(top_control, "NativeWindowHandle", 0) or 0)
        if not hwnd:
            return None
        image = capture_window_rect(hwnd, (left, top, right, bottom))
        point = quote_media_center_in_image(image, direction)
        if point is None:
            return None
        return left + point[0], top + point[1]
    except Exception:
        return None


def quote_media_fallback_point(control, direction: str | None) -> tuple[int, int] | None:
    """Return the lower quote-block click geometry."""
    if direction not in {"friend", "self"}:
        return None
    try:
        rect = control.BoundingRectangle
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None
        # Quoted image thumbnails sit immediately after the quote separator and
        # nickname, much closer to the avatar edge than the former host-side
        # geometry. Keep this as a last-resort point inside the lower quote
        # block; visual detection remains preferred.
        horizontal = min(180, max(108, int(width * 0.105)))
        vertical = min(38, max(26, int(height * 0.12)))
        x = int(rect.left) + horizontal
        if direction == "self":
            x = int(rect.right) - horizontal
        return x, int(rect.bottom) - vertical
    except Exception:
        return None


def majority_direction(samples: list[str | None]) -> str | None:
    """从多次方向采样中取多数结果；平票或全 None 返回 None。"""
    counts: dict[str, int] = {}
    for value in samples:
        if value in ("friend", "self"):
            counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    friend_count = counts.get("friend", 0)
    self_count = counts.get("self", 0)
    if friend_count > self_count:
        return "friend"
    if self_count > friend_count:
        return "self"
    return None


def detect_message_direction_stable(
    control,
    samples: int = 3,
    interval: float = 0.15,
) -> str | None:
    """多次截图采样后按多数投票判断消息方向。

    新消息刚出现时气泡/头像仍在动画中，单次截图容易把 friend 误判成
    None 或 self；连续采样可明显降低监听漏报。
    """
    results: list[str | None] = []
    for _ in range(max(2, samples)):
        results.append(detect_message_direction(control))
        if results.count("friend") >= 2 or results.count("self") >= 2:
            break
        import time

        time.sleep(interval)
    return majority_direction(results)


def detect_message_direction(
    control,
    hwnd: int | None = None,
    debug_dir: str | None = None,
) -> str | None:
    """通过窗口 DC 截图判断消息方向。

    微信 4.x 的消息 ListItem 覆盖整行，UIA 不直接给出左右方向；
    使用 PrintWindow 抓取窗口内容后，比较消息行左右头像区域的
    颜色多样性/方差。朋友消息头像在左，自己消息头像在右。

    返回 ``"friend"``、``"self"`` 或 ``None``（无法判断）。
    """
    try:
        from mabowx.core.win32 import capture_window_rect
    except Exception:
        return None
    try:
        rect = control.BoundingRectangle
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None
        if not hwnd:
            hwnd = int(getattr(control, "NativeWindowHandle", 0) or 0)
        if not hwnd:
            return None
        image = capture_window_rect(
            hwnd,
            (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)),
        ).convert("RGB")
        strip = min(140, width)
        left = image.crop((0, 0, strip, height))
        right = image.crop((width - strip, 0, width, height))

        def stats(region):
            colors = region.getcolors(maxcolors=max(2, region.size[0] * region.size[1]))
            unique = len(colors) if colors else max(2, region.size[0] * region.size[1])
            try:
                from PIL import ImageStat

                variance = sum(ImageStat.Stat(region).var or [0.0])
            except Exception:
                variance = 0.0
            return unique, variance

        left_unique, left_var = stats(left)
        right_unique, right_var = stats(right)

        # 头像通常会让所在一侧产生远高于背景的像素方差。颜色种类数则
        # 很容易被另一侧较长的文字/气泡抬高；真机上曾出现左侧朋友头像
        # 方差高 9 倍、但右侧颜色数较多，旧规则因此把来信判成 self。
        # 先采用强方差信号，方差不够明确时再退回颜色种类数。
        if left_var > right_var * 2 and left_var > 50:
            return "friend"
        if right_var > left_var * 2 and right_var > 50:
            return "self"
        if left_unique > right_unique * 2 and left_unique > 20:
            return "friend"
        if right_unique > left_unique * 2 and right_unique > 20:
            return "self"
        return None
    except Exception:
        return None
