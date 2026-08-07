"""Weather Skill：天气查询能力（Open-Meteo 后端，免费无需 API Key）。

把"查天气"封装为标准化 AI Skill，与 websearch/repo/cicd/issue 同范式。
拆成两个能力（学 ChatFlow "区分当前实况 vs 多日预报"的实践）：
- weather_current：查当前/实时天气，不带 date（查的就是当下）。
- weather_forecast：查指定日期（含未来）预报，带 ISO date——让"明天/后天"
  能被准确查询（planner 在规划阶段把相对日期解析为绝对 ISO 日期传入）。

后端用 Open-Meteo（https://open-meteo.com）：免费、无需注册、无 API Key。
线上要换和风/心知等商业源只需改本类实现，registry 与 agent 完全复用。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .registry import SkillSpec


# WMO weather code → 中文描述（Open-Meteo 返回的 weather_code 用此标准）。
# 见 https://open-meteo.com/en/docs 末尾 "WMO Weather interpretation codes"。
_WMO_CODE_ZH: dict[int, str] = {
    0: "晴", 1: "大致晴朗", 2: "局部多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "毛毛雨（轻）", 53: "毛毛雨（中）", 55: "毛毛雨（强）",
    56: "冻毛毛雨（轻）", 57: "冻毛毛雨（强）",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨（轻）", 67: "冻雨（强）",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "阵雪",
    80: "阵雨（轻）", 81: "阵雨（中）", 82: "阵雨（强）",
    85: "阵雪（轻）", 86: "阵雪（强）",
    95: "雷暴", 96: "雷暴伴小冰雹", 99: "雷暴伴大冰雹",
}


def _wmo_to_zh(code: int) -> str:
    """WMO weather_code 转中文描述，未知 code 回退"未知"。"""
    return _WMO_CODE_ZH.get(int(code), f"未知天气代码 {code}")


# 给 LLM 的厚描述（学 ChatFlow GUIDANCE：what/when/when-not/返回什么）。
GUIDANCE_CURRENT = (
    "【weather_current】查某城市【当前/实时】天气（气温、天气状况、体感温度、"
    "湿度、风速风向）。何时用：用户问'现在/今天天气怎样'且只要当前实况。"
    "何时不用：用户问'明天/后天/下周三/未来几天'的天气——用 weather_forecast。"
    "参数 location 必填，用'城市名'或'城市名, 省/国'格式消歧。"
)
GUIDANCE_FORECAST = (
    "【weather_forecast】查某城市【指定日期】天气预报（含未来日期）。"
    "何时用：用户问'明天/后天/下周三/未来几天天气'——必须先把相对日期"
    "解析为 ISO 8601 绝对日期 YYYY-MM-DD 传入 date。何时不用：用户只要"
    "'现在/今天实况'——用 weather_current。参数 date 必须是绝对日期"
    "（如 2026-08-08），禁止传'明天'原样；location 用'城市名'或"
    "'城市名, 省/国'消歧。"
)


class WeatherSkill:
    """天气查询 Skill：把 Open-Meteo 天气查询封装为标准化 AI 能力。

    拆 current/forecast 两个能力：current 不带 date（查实时），forecast 带
    ISO date（查指定日/未来）。date 兜底解析"今天/明天/后天"中文词，防
    planner 偶发漏解析时仍能工作（解析成功记审计警告，不崩）。
    """

    name = "weather"

    GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    @property
    def available(self) -> bool:
        """Open-Meteo 无需凭证，恒为 True（实际可用性靠网络）。"""
        return True

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _city_of(location: str) -> str:
        """从 location 取城市名：'成都, 四川' → '成都'；'成都' → '成都'。"""
        return (location or "").split(",")[0].strip()

    def _geocode(self, city: str) -> tuple[float, float, str] | None:
        """城市名 → (纬度, 经度, 行政区/国家名)，失败返回 None。"""
        try:
            import httpx
            resp = httpx.get(
                self.GEO_URL,
                params={"name": city, "count": 1, "language": "zh", "format": "json"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") if isinstance(data, dict) else []
            if not results:
                return None
            r = results[0]
            lat = float(r.get("latitude", 0))
            lon = float(r.get("longitude", 0))
            place_parts = [p for p in (r.get("admin1"), r.get("country")) if p]
            place = " ".join(place_parts) or city
            return lat, lon, place
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _parse_date(date_str: str) -> date | None:
        """解析日期为 date 对象。兜底支持 ISO 与中文相对词。

        - ISO 8601 (YYYY-MM-DD) 直接解析。
        - 中文相对词：今天/今日/明天/明日/后天/大后天/大前天 据今天推算。
        - 解析失败返回 None（调用方降级为查当前）。
        """
        s = (date_str or "").strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s)
        except ValueError:
            pass
        today = date.today()
        mapping = {"今天": 0, "今日": 0, "明天": 1, "明日": 1,
                   "后天": 2, "大后天": 3, "大前天": -3}
        for k, v in mapping.items():
            if k in s:
                return today + timedelta(days=v)
        return None

    @staticmethod
    def _wind_dir_zh(deg: float | int | None) -> str:
        """风向角度 → 中文方位（8 方位）。None/异常返回空串。"""
        if deg is None:
            return ""
        try:
            d = float(deg) % 360
        except Exception:  # noqa: BLE001
            return ""
        dirs = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")
        return dirs[int((d + 22.5) // 45) % 8]

    # ------------------------------------------------------------------
    # 能力 1：当前实况
    # ------------------------------------------------------------------
    def get_current_weather(self, location: str) -> str:
        """查某城市当前实时天气，返回中文摘要。

        Args:
            location: 城市名（'成都'/'成都, 四川'/'shanghai' 均可）。
        """
        city = self._city_of(location)
        if not city:
            return "[weather] 未提供城市名。"
        geo = self._geocode(city)
        if geo is None:
            return f"[weather] 未能定位到城市 '{city}'，请用更标准的城市名重试。"
        lat, lon, place = geo
        try:
            import httpx
            resp = httpx.get(
                self.FORECAST_URL,
                params={
                    "latitude": lat, "longitude": lon,
                    "current": ("temperature_2m,relative_humidity_2m,apparent_temperature,"
                                "weather_code,wind_speed_10m,wind_direction_10m"),
                    "timezone": "auto",
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            cur = (resp.json() or {}).get("current") or {}
            if not cur:
                return f"[weather] 已定位 {place}（{lat},{lon}），但未取到天气数据。"
            code = int(cur.get("weather_code", -1))
            lines = [f"{place} 当前天气：{_wmo_to_zh(code)}"]
            if cur.get("temperature_2m") is not None:
                lines.append(f"气温 {cur['temperature_2m']}°C")
            if cur.get("apparent_temperature") is not None:
                lines.append(f"体感 {cur['apparent_temperature']}°C")
            if cur.get("relative_humidity_2m") is not None:
                lines.append(f"湿度 {cur['relative_humidity_2m']}%")
            if cur.get("wind_speed_10m") is not None:
                dz = self._wind_dir_zh(cur.get("wind_direction_10m"))
                lines.append(f"风速 {cur['wind_speed_10m']} km/h" + (f"（{dz}）" if dz else ""))
            return "，".join(lines) + "。"
        except Exception as exc:  # noqa: BLE001
            return f"[weather] 查询天气失败：{exc!r}"

    # ------------------------------------------------------------------
    # 能力 2：指定日期/未来预报
    # ------------------------------------------------------------------
    def get_weather_forecast(self, location: str, date: str = "", days: int = 1) -> str:
        """查某城市指定日期（含未来）天气预报，返回中文摘要。

        Args:
            location: 城市名（'成都'/'成都, 四川'）。
            date: ISO 8601 日期 YYYY-MM-DD。也兼容"今天/明天/后天"中文词兜底。
                空/解析失败则查未来 days 天（不锁定某日）。
            days: 要几天预报，默认 1。Open-Meteo 最多未来 16 天。
        """
        city = self._city_of(location)
        if not city:
            return "[weather] 未提供城市名。"
        geo = self._geocode(city)
        if geo is None:
            return f"[weather] 未能定位到城市 '{city}'，请用更标准的城市名重试。"
        lat, lon, place = geo

        # 日期解析：ISO 优先，中文词兜底。
        target = self._parse_date(date)
        params: dict = {
            "latitude": lat, "longitude": lon,
            "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                      "apparent_temperature_max,apparent_temperature_min,"
                      "precipitation_sum,precipitation_probability_max,"
                      "wind_speed_10m_max,wind_direction_10m_dominant"),
            "timezone": "auto",
        }
        note = ""
        if target is not None:
            params["start_date"] = target.isoformat()
            params["end_date"] = (target + timedelta(days=max(1, days) - 1)).isoformat()
        elif date:
            # 传了 date 但解析失败——降级查未来 days 天并提示。
            note = f"[日期'{date}'未能解析为绝对日期，改为查未来 {days} 天预报] "
        try:
            import httpx
            resp = httpx.get(self.FORECAST_URL, params=params, timeout=self._timeout)
            resp.raise_for_status()
            daily = (resp.json() or {}).get("daily") or {}
            times = daily.get("time") or []
            if not times:
                return f"[weather] 已定位 {place}，但未取到预报数据。"
            # 拼逐日摘要。
            day_lines = []
            for i, d in enumerate(times):
                code = int(daily.get("weather_code", [0])[i] if i < len(daily.get("weather_code", [])) else -1)
                tmax = _safe_idx(daily.get("temperature_2m_max"), i)
                tmin = _safe_idx(daily.get("temperature_2m_min"), i)
                amax = _safe_idx(daily.get("apparent_temperature_max"), i)
                amin = _safe_idx(daily.get("apparent_temperature_min"), i)
                pop = _safe_idx(daily.get("precipitation_probability_max"), i)
                psum = _safe_idx(daily.get("precipitation_sum"), i)
                wmax = _safe_idx(daily.get("wind_speed_10m_max"), i)
                wdir = _safe_idx(daily.get("wind_direction_10m_dominant"), i)
                parts = [f"{d}：{_wmo_to_zh(code)}"]
                if tmax is not None and tmin is not None:
                    parts.append(f"{tmin}~{tmax}°C")
                if amax is not None and amin is not None:
                    parts.append(f"体感 {amin}~{amax}°C")
                if pop is not None:
                    parts.append(f"降水概率 {pop}%")
                if psum is not None and psum != 0:
                    parts.append(f"降水量 {psum}mm")
                if wmax is not None:
                    dz = self._wind_dir_zh(wdir)
                    parts.append(f"最大风速 {wmax} km/h" + (f"（{dz}）" if dz else ""))
                day_lines.append("，".join(parts))
            return f"{place} 天气预报：\n" + "\n".join(day_lines) + (f"\n{note}" if note else "")
        except Exception as exc:  # noqa: BLE001
            return f"[weather] 查询预报失败：{exc!r}"

    # ------------------------------------------------------------------
    # Skill 能力清单
    # ------------------------------------------------------------------
    def specs(self) -> list[SkillSpec]:
        """暴露 current/forecast 两个能力，均带 guidance 厚描述。"""
        return [
            SkillSpec(
                name="weather_current",
                description="查询指定城市的实时/当前天气。入参 location。",
                func=self.get_current_weather,
                guidance=GUIDANCE_CURRENT,
                schema={
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "城市名（'成都'/'成都, 四川'）"},
                    },
                    "required": ["location"],
                },
            ),
            SkillSpec(
                name="weather_forecast",
                description="查询指定城市指定日期（含未来）的天气预报。入参 location + date(ISO YYYY-MM-DD)。",
                func=self.get_weather_forecast,
                guidance=GUIDANCE_FORECAST,
                schema={
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "城市名（'成都'/'北京, 中国'）"},
                        "date": {"type": "string",
                                 "description": "ISO 8601 绝对日期 YYYY-MM-DD（如 2026-08-08）。"
                                                "必须把相对日期（明天/后天）据今天解析后传入，禁止传'明天'原样"},
                        "days": {"type": "integer", "description": "要几天的预报，默认1", "default": 1},
                    },
                    "required": ["location", "date"],
                },
            ),
        ]


def _safe_idx(lst, i):
    """安全取列表第 i 项，越界/异常返回 None。"""
    try:
        return lst[i]
    except Exception:  # noqa: BLE001
        return None
