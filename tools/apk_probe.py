"""APK 体检：不依赖 aapt / androguard，直接读二进制 AndroidManifest.xml。

用途：在决定「模拟器能不能跑」之前，先把包名、版本、SDK、权限、
ABI、加固特征、模拟器检测关键字这些硬信息拿出来。
"""
from __future__ import annotations

import collections
import re
import struct
import sys
import zipfile
from pathlib import Path

# ---------- AXML 解析 ----------

RES_STRING_POOL_TYPE = 0x0001
RES_XML_TYPE = 0x0003
RES_XML_START_NAMESPACE_TYPE = 0x0100
RES_XML_END_NAMESPACE_TYPE = 0x0101
RES_XML_START_ELEMENT_TYPE = 0x0102
RES_XML_END_ELEMENT_TYPE = 0x0103
RES_XML_RESOURCE_MAP_TYPE = 0x0180

# 常见属性名 -> 资源 id（AXML 里属性名常被编译成 id，字符串池拿不到原名）
ATTR_IDS = {
    0x0101021B: "versionCode",
    0x0101021C: "versionName",
    0x0101020C: "minSdkVersion",
    0x01010270: "targetSdkVersion",
    0x01010271: "maxSdkVersion",
    0x01010003: "name",
    0x01010010: "exported",
    0x01010018: "authorities",
    0x01010572: "compileSdkVersionCodename",
    0x01010573: "compileSdkVersion",
    0x01010024: "value",
    0x01010025: "resource",
}


def _read_string_pool(data: bytes, off: int):
    (_, header_size, chunk_size) = struct.unpack_from("<HHI", data, off)
    (count, style_count, flags, strings_start, styles_start) = struct.unpack_from(
        "<IIIII", data, off + 8
    )
    utf8 = bool(flags & 0x100)
    offsets = struct.unpack_from(f"<{count}I", data, off + header_size)
    base = off + strings_start
    out: list[str] = []
    for o in offsets:
        p = base + o
        if utf8:
            # 先读 UTF-16 字符数（1~2 字节），再读 1~2 字节的字节长度
            n = data[p]
            p += 2 if n & 0x80 else 1
            n = data[p]
            if n & 0x80:
                n = ((n & 0x7F) << 8) | data[p + 1]
                p += 2
            else:
                p += 1
            out.append(data[p : p + n].decode("utf-8", "replace"))
        else:
            n = struct.unpack_from("<H", data, p)[0]
            p += 2
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, p)[0]
                p += 2
            out.append(data[p : p + n * 2].decode("utf-16-le", "replace"))
    return out, chunk_size


def parse_manifest(xml: bytes) -> dict:
    """返回 {tag: [ {attr: value}, ... ]}，够用就好。"""
    magic, _, total = struct.unpack_from("<HHI", xml, 0)
    if magic != RES_XML_TYPE:
        raise ValueError("不是二进制 AXML")
    strings: list[str] = []
    res_map: list[int] = []
    tags: dict[str, list[dict]] = collections.defaultdict(list)

    off = 8
    while off < total:
        ctype, header_size, chunk_size = struct.unpack_from("<HHI", xml, off)
        if chunk_size <= 0:
            break
        if ctype == RES_STRING_POOL_TYPE:
            strings, _ = _read_string_pool(xml, off)
        elif ctype == RES_XML_RESOURCE_MAP_TYPE:
            n = (chunk_size - header_size) // 4
            res_map = list(struct.unpack_from(f"<{n}I", xml, off + header_size))
        elif ctype == RES_XML_START_ELEMENT_TYPE:
            body = off + header_size
            (ns, name_idx, attr_start, attr_size, attr_count) = struct.unpack_from(
                "<IIHHH", xml, body
            )
            name = strings[name_idx] if name_idx < len(strings) else f"<{name_idx}>"
            attrs: dict[str, str] = {}
            aoff = body + attr_start
            for i in range(attr_count):
                aos = aoff + i * attr_size
                ans, aname, raw = struct.unpack_from("<III", xml, aos)
                _tsize, _res0, dtype, adata = struct.unpack_from("<HBBI", xml, aos + 12)
                key = None
                if aname < len(strings) and strings[aname]:
                    key = strings[aname]
                if key is None and aname < len(res_map):
                    key = ATTR_IDS.get(res_map[aname])
                key = key or f"attr{hex(aname)}"
                if raw != 0xFFFFFFFF and raw < len(strings):
                    val = strings[raw]
                elif dtype == 0x03 and adata < len(strings):
                    val = strings[adata]
                elif dtype == 0x12:
                    val = "true" if adata else "false"
                elif dtype in (0x10, 0x11):
                    val = str(adata)
                elif dtype == 0x01:
                    val = f"@ref/0x{adata:08x}"
                else:
                    val = f"0x{adata:x}"
                attrs[key] = val
            tags[name].append(attrs)
        off += chunk_size
    return tags


# ---------- 加固 / 模拟器检测特征 ----------

PACKERS = {
    "libjniload.so": "爱加密(ijiami)",
    "libzxprotect.so": "自研/第三方 protect",
    "libDexHelper.so": "梆梆加固",
    "libjiagu.so": "360 加固",
    "libshella-": "腾讯乐固",
    "libmobisec": "阿里聚安全",
    "libnesec.so": "网易易盾",
    "libbaiduprotect": "百度加固",
}

EMU_TOKENS = [
    b"goldfish", b"ranchu", b"qemu", b"Genymotion", b"generic_x86",
    b"BlueStacks", b"bluestacks", b"nox", b"Nox", b"MicroVirt",
    b"TTVM", b"andy", b"droid4x", b"ro.kernel.qemu", b"init.svc.qemud",
    b"isEmulator", b"isRooted", b"su_check",
]


def main() -> int:
    apk = Path(sys.argv[1] if len(sys.argv) > 1 else "sxz_app_release(1).apk")
    z = zipfile.ZipFile(apk)
    print(f"文件：{apk.name}  {apk.stat().st_size / 1048576:.1f} MB")

    xml = z.read("AndroidManifest.xml")
    tags = parse_manifest(xml)

    man = tags["manifest"][0]
    print("\n=== 基本信息 ===")
    for k in ("package", "versionName", "versionCode", "compileSdkVersion"):
        if k in man:
            print(f"{k:20s} {man[k]}")
    uses = tags.get("uses-sdk", [{}])[0]
    print(f"{'minSdkVersion':20s} {uses.get('minSdkVersion', '?')}")
    print(f"{'targetSdkVersion':20s} {uses.get('targetSdkVersion', '?')}")

    app = tags.get("application", [{}])[0]
    print(f"{'application':20s} {app.get('name', '?')}")
    print(f"{'debuggable':20s} {app.get('debuggable', 'false')}")

    # 启动 activity：找 MAIN + LAUNCHER
    launcher = None
    for act in tags.get("activity", []) + tags.get("activity-alias", []):
        for f in tags.get("intent-filter", []):
            pass
    # intent-filter 的父子关系 AXML 里没保留，只能按「有 MAIN/LAUNCHER action」粗判
    actions = {a.get("name") for a in tags.get("action", [])}
    cats = {c.get("name") for c in tags.get("category", [])}
    if "android.intent.category.LAUNCHER" in cats:
        for act in tags.get("activity", []):
            launcher = act.get("name")
            break
    print(f"{'launcher':20s} {launcher or '(未解析到)'}")
    print(f"{'actions':20s} {sorted(a for a in actions if a)[:6]}")

    perms = [p.get("name", "") for p in tags.get("uses-permission", [])]
    perms += [p.get("name", "") for p in tags.get("uses-permission-sdk-23", [])]
    hot = [p for p in perms if any(k in p for k in (
        "CAMERA", "LOCATION", "RECORD_AUDIO", "READ_PHONE", "WRITE_EXTERNAL"))]
    print(f"\n=== 权限 {len(perms)} 个（敏感 {len(hot)}）===")
    for p in hot:
        print("  ", p)

    # ABI
    names = z.namelist()
    abis = collections.Counter(n.split("/")[1] for n in names if n.startswith("lib/") and n.count("/") >= 2 and len(n.split("/")) > 2)
    print(f"\n=== ABI ===\n  {dict(abis)}")
    if not {"x86", "x86_64"} & set(abis):
        print("  结论：无 x86/x86_64 原生库 → x86 模拟器必须靠 ARM 转译")

    # 加固
    print("\n=== 加固特征 ===")
    hit = []
    for n in names:
        base = n.rsplit("/", 1)[-1]
        for sig, label in PACKERS.items():
            if base.startswith(sig) or base == sig:
                hit.append(f"{n}  →  {label}")
    print("  " + ("\n  ".join(hit) if hit else "未发现常见加固 so"))

    # 框架判断
    print("\n=== UI 框架 ===")
    if "lib/arm64-v8a/libflutter.so" in names:
        print("  Flutter（libflutter.so + libapp.so）→ 自绘 UI，无原生 EditText")
    if any(n.endswith("libneonuijni.so") for n in names):
        print("  含 neonui（自研/第三方 UI 组件库）")
    print(f"  dex 数量：{sum(1 for n in names if n.endswith('.dex'))}")

    # 模拟器检测关键字：只扫 dex 和几个可疑 so，避免全量慢
    print("\n=== 模拟器/root 检测关键字（命中即说明有主动对抗）===")
    targets = [n for n in names if n.endswith(".dex")] + [
        n for n in names if n.endswith(("libzxprotect.so", "libjniload.so"))
    ]
    for n in targets:
        try:
            blob = z.read(n)
        except Exception:
            continue
        found = sorted({t.decode() for t in EMU_TOKENS if t in blob})
        if found:
            print(f"  {n}: {', '.join(found)}")
    print("\n（dex 可能是加密的；扫不到不代表没有）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
