import asyncio
import base64
import json
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin

import httpx
from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from .utils.file_send_server import send_file
except ImportError:
    plugin_dir = Path(__file__).parent
    plugin_dir_str = str(plugin_dir)
    if plugin_dir_str not in sys.path:
        sys.path.append(plugin_dir_str)
    try:
        from utils.file_send_server import send_file  # type: ignore
    except ImportError:
        send_file = None
        logger.warning("NapCat 文件转发模块未找到，将跳过 NapCat 中转功能")


@register("grok-image-edit", "Claude", "Grok图片编辑插件，支持根据图片和提示词编辑图片", "1.0.0")
class GrokImageEditPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

        # API配置
        self.server_url = config.get("server_url", "https://api.x.ai").rstrip("/")
        self.model_id = config.get("model_id", "grok-imagine-0.9")
        self.api_key = config.get("api_key", "")
        self.enabled = config.get("enabled", True)
        self.prompt_prefix = str(
            config.get(
                "prompt_prefix",
                "请基于所附图片进行编辑，不要生成全新图像。尽量保持主体、构图、视角和背景不变，只做提示词要求的修改。",
            )
        ).strip()
        self.status_message_mode = str(config.get("status_message_mode", "minimal")).strip().lower()
        if self.status_message_mode not in {"verbose", "minimal", "silent"}:
            self.status_message_mode = "minimal"

        # 请求配置
        self.timeout_seconds = config.get("timeout_seconds", 120)
        self.max_retry_attempts = config.get("max_retry_attempts", 3)

        # 群组控制
        self.group_control_mode = config.get("group_control_mode", "off").lower()
        self.group_list = list(config.get("group_list", []))

        # 速率限制
        self.rate_limit_enabled = config.get("rate_limit_enabled", True)
        self.rate_limit_window_seconds = config.get("rate_limit_window_seconds", 3600)
        self.rate_limit_max_calls = config.get("rate_limit_max_calls", 5)
        self._rate_limit_bucket = {}
        self._rate_limit_locks = {}
        self._processing_tasks = {}

        # 管理员用户
        self.admin_users = set(str(u) for u in config.get("admin_users", []))

        # 图片结果处理
        self.max_images_per_response = int(config.get("max_images_per_response", 4))
        self.save_image_enabled = config.get("save_image_enabled", False)

        self.nap_server_address = (config.get("nap_server_address") or "").strip()
        nap_port = config.get("nap_server_port")
        try:
            self.nap_server_port = int(nap_port)
        except (TypeError, ValueError):
            self.nap_server_port = 0

        # 使用 AstrBot data 目录保存图片
        try:
            plugin_data_dir = Path(StarTools.get_data_dir("astrbot_plugin_grok_image_edit"))
            self.images_dir = plugin_data_dir / "images"
            self.images_dir.mkdir(parents=True, exist_ok=True)
            self.images_dir = self.images_dir.resolve()
        except Exception as e:
            logger.warning(f"无法使用StarTools数据目录，使用插件目录: {e}")
            self.images_dir = Path(__file__).parent / "images"
            self.images_dir.mkdir(parents=True, exist_ok=True)
            self.images_dir = self.images_dir.resolve()

        # API URL
        self.api_url = urljoin(self.server_url + "/", "v1/chat/completions")

        logger.info(f"Grok图片编辑插件已初始化，API地址: {self.api_url}")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return str(event.get_sender_id()) in self.admin_users

    async def _check_group_access(self, event: AstrMessageEvent) -> Optional[str]:
        """检查群组访问权限和速率限制（并发安全）"""
        try:
            group_id = None
            try:
                group_id = event.get_group_id()
            except Exception:
                group_id = None

            if group_id:
                if self.group_control_mode == "whitelist" and group_id not in self.group_list:
                    return "当前群组未被授权使用图片编辑功能"
                if self.group_control_mode == "blacklist" and group_id in self.group_list:
                    return "当前群组已被限制使用图片编辑功能"

                if self.rate_limit_enabled:
                    if group_id not in self._rate_limit_locks:
                        self._rate_limit_locks[group_id] = asyncio.Lock()

                    async with self._rate_limit_locks[group_id]:
                        now = time.time()
                        bucket = self._rate_limit_bucket.get(group_id, {"window_start": now, "count": 0})
                        window_start = bucket.get("window_start", now)
                        count = int(bucket.get("count", 0))

                        if now - window_start >= self.rate_limit_window_seconds:
                            window_start = now
                            count = 0

                        if count >= self.rate_limit_max_calls:
                            return (
                                f"本群调用已达上限（{self.rate_limit_max_calls}次/"
                                f"{self.rate_limit_window_seconds}秒），请稍后再试"
                            )

                        bucket["window_start"], bucket["count"] = window_start, count + 1
                        self._rate_limit_bucket[group_id] = bucket

        except Exception as e:
            logger.error(f"群组访问检查失败: {e}")
            return None

        return None

    async def _extract_images_from_message(self, event: AstrMessageEvent) -> Tuple[List[str], Optional[str]]:
        """从消息中提取图片的base64数据"""
        images: List[str] = []
        reply_ids: List[str] = []

        if hasattr(event, "message_obj") and event.message_obj and hasattr(event.message_obj, "message"):
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    try:
                        base64_data = await comp.convert_to_base64()
                        if base64_data:
                            if not base64_data.startswith("data:"):
                                base64_data = f"data:image/jpeg;base64,{base64_data}"
                            images.append(base64_data)
                    except Exception as e:
                        logger.warning(f"图片转base64失败: {e}")
                elif isinstance(comp, Reply):
                    if comp.chain:
                        for reply_comp in comp.chain:
                            if isinstance(reply_comp, Image):
                                try:
                                    base64_data = await reply_comp.convert_to_base64()
                                    if base64_data:
                                        if not base64_data.startswith("data:"):
                                            base64_data = f"data:image/jpeg;base64,{base64_data}"
                                        images.append(base64_data)
                                except Exception as e:
                                    logger.warning(f"引用图片转base64失败: {e}")
                    reply_id = self._extract_reply_message_id(comp)
                    if reply_id:
                        reply_ids.append(str(reply_id))

        if images:
            return images, None

        if reply_ids:
            last_error: Optional[str] = None
            for reply_id in reply_ids:
                fetched, error = await self._fetch_images_from_reply(event, reply_id)
                if fetched:
                    return fetched, None
                if error:
                    last_error = error
            return [], last_error or "引用消息中未找到图片"

        return [], None

    def _extract_reply_message_id(self, reply_comp: Reply) -> Optional[str]:
        for attr in ("message_id", "id", "reply_id", "msg_id"):
            try:
                val = getattr(reply_comp, attr, None)
            except Exception:
                val = None
            if isinstance(val, (str, int)) and str(val).strip():
                return str(val)

        for attr in ("data", "raw", "_data"):
            try:
                data = getattr(reply_comp, attr, None)
            except Exception:
                data = None
            if isinstance(data, dict):
                for key in ("message_id", "id", "reply_id", "msg_id"):
                    val = data.get(key)
                    if isinstance(val, (str, int)) and str(val).strip():
                        return str(val)

        return None

    def _get_platform_name(self, event: AstrMessageEvent) -> Optional[str]:
        for attr in ("get_platform_name", "platform_name", "platform"):
            try:
                val = getattr(event, attr, None)
            except Exception:
                val = None
            if val is None:
                continue
            try:
                return val() if callable(val) else str(val)
            except Exception:
                continue
        return None

    def _get_onebot_api(self, event: AstrMessageEvent):
        platform = self._get_platform_name(event)
        if platform and platform.lower() not in ("aiocqhttp", "onebot11", "onebot-11", "onebot"):
            return None

        bot = getattr(event, "bot", None)
        if bot is None:
            return None

        api = getattr(bot, "api", None)
        if api is not None and hasattr(api, "call_action"):
            return api
        if hasattr(bot, "call_action"):
            return bot
        return None

    async def _fetch_images_from_reply(
        self, event: AstrMessageEvent, reply_id: str
    ) -> Tuple[List[str], Optional[str]]:
        api = self._get_onebot_api(event)
        if api is None:
            return [], "当前适配器不支持通过引用消息获取图片"

        try:
            message_id = int(reply_id) if str(reply_id).isdigit() else reply_id
        except Exception:
            message_id = reply_id

        try:
            result = await api.call_action("get_msg", message_id=message_id)
        except Exception as e:
            return [], f"获取引用消息失败: {str(e)}"

        if not isinstance(result, dict):
            return [], "获取引用消息失败: 返回格式异常"

        status = result.get("status")
        retcode = result.get("retcode")
        if status and status != "ok":
            return [], f"获取引用消息失败: status={status}"
        if retcode not in (None, 0):
            return [], f"获取引用消息失败: retcode={retcode}"

        data = result.get("data") if isinstance(result.get("data"), dict) else result
        if not isinstance(data, dict):
            return [], "获取引用消息失败: 返回数据异常"

        message = data.get("message")
        if not isinstance(message, list):
            return [], "引用消息message格式不支持解析"

        images: List[str] = []
        errors: List[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") != "image":
                continue
            data_field = segment.get("data")
            if not isinstance(data_field, dict):
                continue
            data_url, error = await self._onebot_image_data_to_data_url(data_field, api)
            if data_url:
                images.append(data_url)
            elif error:
                errors.append(error)

        if images:
            return images, None
        if errors:
            return [], f"引用图片解析失败: {errors[0]}"
        return [], "引用消息中未找到图片"

    async def _onebot_image_data_to_data_url(self, data: dict, api) -> Tuple[Optional[str], Optional[str]]:
        base64_data = data.get("base64") or data.get("b64")
        if isinstance(base64_data, str) and base64_data:
            return f"data:image/jpeg;base64,{base64_data}", None

        url = data.get("url")
        if isinstance(url, str) and url:
            data_url = await self._download_image_as_data_url(url)
            if data_url:
                return data_url, None
            return None, "下载引用图片失败"

        file_id = data.get("file") or data.get("file_id")
        if isinstance(file_id, (str, int)) and str(file_id):
            file_id = str(file_id)
            if file_id.startswith("base64://"):
                return f"data:image/jpeg;base64,{file_id[len('base64://'):]}", None
            image_url = await self._get_onebot_image_url(api, file_id)
            if image_url:
                data_url = await self._download_image_as_data_url(image_url)
                if data_url:
                    return data_url, None
                return None, "下载引用图片失败"

        path = data.get("path")
        if isinstance(path, str) and path:
            try:
                file_path = Path(path)
                if file_path.exists() and file_path.is_file():
                    image_bytes = file_path.read_bytes()
                    b64 = base64.b64encode(image_bytes).decode()
                    return f"data:image/jpeg;base64,{b64}", None
            except Exception as e:
                return None, f"读取引用图片文件失败: {e}"

        return None, "引用图片段不包含可用的url或file"

    async def _get_onebot_image_url(self, api, file_id: str) -> Optional[str]:
        try:
            result = await api.call_action("get_image", file=file_id)
        except Exception:
            return None

        if not isinstance(result, dict):
            return None
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        if not isinstance(data, dict):
            return None

        url = data.get("url") or data.get("download_url")
        if isinstance(url, str) and url:
            return url
        return None

    async def _download_image_as_data_url(self, image_url: str) -> Optional[str]:
        try:
            timeout_config = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=60.0)
            async with httpx.AsyncClient(timeout=timeout_config, follow_redirects=True) as client:
                response = await client.get(image_url)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";")[0].strip()
                if not content_type.startswith("image/"):
                    content_type = "image/jpeg"
                b64 = base64.b64encode(response.content).decode()
                return f"data:{content_type};base64,{b64}"
        except Exception as e:
            logger.warning(f"下载引用图片失败: {e}")
            return None

    async def _call_grok_api(self, prompt: str, image_base64: str) -> Tuple[List[str], List[str], Optional[str]]:
        """调用 Grok API 进行图片编辑"""
        if not self.api_key:
            return [], [], "未配置API密钥"

        final_prompt = prompt.strip() if isinstance(prompt, str) else str(prompt)
        if self.prompt_prefix:
            final_prompt = f"{self.prompt_prefix}\n{final_prompt}" if final_prompt else self.prompt_prefix

        payload = {
            "model": self.model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": final_prompt},
                        {"type": "image_url", "image_url": {"url": image_base64}},
                    ],
                }
            ],
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        timeout_config = httpx.Timeout(
            connect=10.0,
            read=self.timeout_seconds,
            write=10.0,
            pool=self.timeout_seconds + 10,
        )

        for attempt in range(self.max_retry_attempts):
            try:
                logger.info(f"调用Grok API (尝试 {attempt + 1}/{self.max_retry_attempts})")
                logger.debug(f"请求URL: {self.api_url}")
                logger.debug(f"请求模型: {self.model_id}")

                async with httpx.AsyncClient(timeout=timeout_config) as client:
                    response = await client.post(self.api_url, json=payload, headers=headers)

                logger.info(f"API响应状态码: {response.status_code}")
                response_text = response.text
                logger.debug(f"API响应内容: {response_text[:500]}...")

                if response.status_code == 200:
                    try:
                        result = response.json()
                        logger.debug(f"解析的JSON响应: {result}")

                        image_urls, data_urls, parse_error = self._extract_image_results(result)
                        if parse_error:
                            return [], [], parse_error

                        if image_urls or data_urls:
                            return image_urls, data_urls, None
                        return [], [], "API响应中未包含有效的图片链接"
                    except json.JSONDecodeError as e:
                        return [], [], f"API响应JSON解析失败: {str(e)}, 响应内容: {response_text[:200]}"

                if response.status_code == 403:
                    return [], [], "API访问被拒绝，请检查密钥和权限"

                error_msg = f"API请求失败 (状态码: {response.status_code})"
                try:
                    error_detail = response.json()
                    logger.debug(f"错误详情JSON: {error_detail}")
                    if "error" in error_detail:
                        error_msg += f": {error_detail['error']}"
                    elif "message" in error_detail:
                        error_msg += f": {error_detail['message']}"
                    else:
                        error_msg += f": {error_detail}"
                except Exception:
                    error_msg += f": {response_text[:200]}"

                if attempt == self.max_retry_attempts - 1:
                    return [], [], error_msg

                logger.warning(f"{error_msg}，等待重试...")
                await asyncio.sleep(2)

            except httpx.TimeoutException:
                error_msg = f"请求超时 ({self.timeout_seconds}秒)"
                if attempt == self.max_retry_attempts - 1:
                    return [], [], error_msg
                logger.warning(f"{error_msg}，等待重试...")
                await asyncio.sleep(1)

            except Exception as e:
                error_msg = f"请求异常: {str(e)}"
                if attempt == self.max_retry_attempts - 1:
                    return [], [], error_msg
                logger.warning(f"{error_msg}，等待重试...")
                await asyncio.sleep(1)

        return [], [], "所有重试均失败"

    def _extract_image_results(self, response_data: dict) -> Tuple[List[str], List[str], Optional[str]]:
        """从响应中提取图片 URL 或 base64 数据"""
        try:
            if not isinstance(response_data, dict):
                return [], [], f"无效的响应格式: {type(response_data)}"

            image_urls: List[str] = []
            data_urls: List[str] = []

            # 结构化 data 字段（OpenAI images 兼容）
            data_field = response_data.get("data")
            if isinstance(data_field, list):
                for item in data_field:
                    if not isinstance(item, dict):
                        continue
                    url = item.get("url")
                    if isinstance(url, str) and self._is_valid_image_url(url, require_extension=False):
                        image_urls.append(url)
                    b64_json = item.get("b64_json")
                    if isinstance(b64_json, str) and b64_json:
                        data_urls.append(f"data:image/png;base64,{b64_json}")

            # choices -> message
            choice = None
            if "choices" in response_data and isinstance(response_data["choices"], list):
                if response_data["choices"]:
                    choice = response_data["choices"][0]

            if isinstance(choice, dict):
                message = choice.get("message", {})
                content = message.get("content")

                # 结构化内容
                if isinstance(content, list):
                    text_parts: List[str] = []
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "image_url":
                            image_url = None
                            image_url_field = part.get("image_url")
                            if isinstance(image_url_field, dict):
                                image_url = image_url_field.get("url")
                            elif isinstance(part.get("url"), str):
                                image_url = part.get("url")
                            if isinstance(image_url, str) and self._is_valid_image_url(image_url, require_extension=False):
                                image_urls.append(image_url)
                        if part.get("type") == "text" and isinstance(part.get("text"), str):
                            text_parts.append(part["text"])

                    if text_parts:
                        content_text = "\n".join(text_parts)
                        image_urls.extend(self._extract_image_urls_from_text(content_text))
                        data_urls.extend(self._extract_data_urls_from_text(content_text))

                elif isinstance(content, str):
                    image_urls.extend(self._extract_image_urls_from_text(content))
                    data_urls.extend(self._extract_data_urls_from_text(content))

                # 兼容 attachments/media
                for field in ["attachments", "media", "files", "images"]:
                    items = message.get(field)
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict) and isinstance(item.get("url"), str):
                                url = item["url"]
                                if self._is_valid_image_url(url, require_extension=False):
                                    image_urls.append(url)

            image_urls = self._dedupe_preserve(image_urls)
            data_urls = self._dedupe_preserve(data_urls)

            if not image_urls and not data_urls:
                return [], [], "未能从 API 响应中提取到有效图片"

            return image_urls, data_urls, None

        except Exception as e:
            logger.error(f"图片结果提取异常: {e}")
            return [], [], f"图片提取失败: {str(e)}"

    def _dedupe_preserve(self, items: List[str]) -> List[str]:
        seen = set()
        result = []
        for item in items:
            if not item or item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result

    def _extract_image_urls_from_text(self, content: str) -> List[str]:
        if not content:
            return []

        urls: List[str] = []

        # HTML img 标签
        html_pattern = r'<img[^>]*src=["\']([^"\']+)["\'][^>]*>'
        for match in re.findall(html_pattern, content, re.IGNORECASE):
            if self._is_valid_image_url(match, require_extension=False):
                urls.append(match)

        # Markdown 图片
        md_pattern = r'!\[[^\]]*\]\(([^\)]+)\)'
        for match in re.findall(md_pattern, content):
            if self._is_valid_image_url(match, require_extension=False):
                urls.append(match)

        # 直接 URL（带图片扩展名）
        direct_pattern = r"(https?://[^\s<>\"\')\]\}]+\.(?:png|jpg|jpeg|webp|gif)(?:\?[^\s<>\"\')\]\}]*)?)"
        for match in re.findall(direct_pattern, content, re.IGNORECASE):
            if self._is_valid_image_url(match, require_extension=True):
                urls.append(match)

        return urls

    def _extract_data_urls_from_text(self, content: str) -> List[str]:
        if not content:
            return []
        pattern = r'(data:image/(?:png|jpeg|jpg|webp|gif);base64,[A-Za-z0-9+/=]+)'
        return re.findall(pattern, content, re.IGNORECASE)

    def _is_valid_image_url(self, url: str, require_extension: bool) -> bool:
        if not isinstance(url, str) or len(url) < 10:
            return False
        if not url.startswith(("http://", "https://")):
            return False
        if require_extension:
            if not re.search(r'\.(png|jpg|jpeg|webp|gif)(?:$|[?&#])', url, re.IGNORECASE):
                return False
        invalid_chars = ["<", ">", '"', "'", "\n", "\r", "\t"]
        if any(char in url for char in invalid_chars):
            return False
        return True

    async def _download_image(self, image_url: str) -> Optional[str]:
        """下载图片到本地"""
        try:
            ext_match = re.search(r'\.(png|jpg|jpeg|webp|gif)(?:$|[?&#])', image_url, re.IGNORECASE)
            ext = ext_match.group(1).lower() if ext_match else "png"
            filename = f"grok_image_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.{ext}"
            file_path = self.images_dir / filename

            timeout_config = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=120.0)

            async with httpx.AsyncClient(timeout=timeout_config) as client:
                response = await client.get(image_url)
                response.raise_for_status()
                with open(file_path, "wb") as f:
                    f.write(response.content)

            absolute_path = file_path.resolve()
            logger.info(f"图片已保存到: {absolute_path}")
            return str(absolute_path)

        except Exception as e:
            logger.error(f"下载图片失败: {e}")
            return None

    def _save_base64_image(self, data_url: str) -> Optional[str]:
        """保存 base64 图片到本地"""
        try:
            match = re.match(r"data:image/([a-zA-Z0-9.+-]+);base64,(.+)", data_url, re.DOTALL)
            if not match:
                return None
            ext = match.group(1).lower()
            b64_data = match.group(2)
            image_bytes = base64.b64decode(b64_data, validate=True)

            filename = f"grok_image_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.{ext}"
            file_path = self.images_dir / filename
            with open(file_path, "wb") as f:
                f.write(image_bytes)

            absolute_path = file_path.resolve()
            logger.info(f"base64图片已保存到: {absolute_path}")
            return str(absolute_path)
        except Exception as e:
            logger.error(f"保存 base64 图片失败: {e}")
            return None

    async def _prepare_image_path(self, image_path: str) -> str:
        if not image_path:
            return image_path
        if not (self.nap_server_address and self.nap_server_port):
            return image_path
        if send_file is None:
            logger.debug("NapCat 文件转发模块不可用，直接返回本地路径")
            return image_path
        try:
            forwarded_path = await send_file(image_path, self.nap_server_address, self.nap_server_port)
            if forwarded_path:
                logger.info(f"NapCat file server returned image path: {forwarded_path}")
                return forwarded_path
            logger.warning("NapCat file server did not return a valid image path, falling back to local file")
        except Exception as e:
            logger.warning(f"NapCat file server transfer failed, falling back to local file: {e}")
        return image_path

    async def _cleanup_image_file(self, image_path: Optional[str]):
        if not image_path:
            return
        if self.save_image_enabled:
            return
        try:
            path = Path(image_path)
            if path.exists():
                path.unlink()
                logger.debug(f"已清理本地图片缓存: {path}")
        except Exception as e:
            logger.warning(f"清理图片文件失败: {e}")

    async def _generate_image_edit_core(
        self,
        event: AstrMessageEvent,
        prompt: str,
        prefetched_images: Optional[List[str]] = None,
        prefetched_error: Optional[str] = None,
    ) -> Tuple[List[str], List[str], Optional[str]]:
        """核心图片编辑逻辑"""
        if not self.enabled:
            return [], [], "图片编辑功能已禁用"

        if prefetched_error:
            return [], [], prefetched_error

        if prefetched_images is None:
            images, extract_error = await self._extract_images_from_message(event)
            if extract_error:
                return [], [], extract_error
        else:
            images = prefetched_images

        if not images:
            return [], [], "未找到图片，请在消息中包含图片或引用包含图片的消息"

        image_base64 = images[0]
        image_urls, data_urls, error_msg = await self._call_grok_api(prompt, image_base64)
        if error_msg:
            return [], [], error_msg

        image_paths: List[str] = []
        for data_url in data_urls:
            saved = self._save_base64_image(data_url)
            if saved:
                image_paths.append(saved)

        return image_urls, image_paths, None

    async def _async_edit_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        task_id: str,
        prefetched_images: Optional[List[str]] = None,
        prefetched_error: Optional[str] = None,
    ):
        user_id = str(event.get_sender_id())
        try:
            logger.info(f"开始处理用户 {user_id} 的图片编辑任务: {task_id}")

            image_urls, image_paths, error_msg = await self._generate_image_edit_core(
                event, prompt, prefetched_images=prefetched_images, prefetched_error=prefetched_error
            )

            if error_msg:
                await event.send(event.plain_result(f"❌ {error_msg}"))
                return

            if not image_urls and not image_paths:
                await event.send(event.plain_result("❌ 图片编辑失败，请稍后再试"))
                return

            if self.status_message_mode == "verbose":
                await event.send(event.plain_result("🖼️ 正在发送编辑后的图片，请稍候..."))

            components = []
            count = 0

            for image_url in image_urls:
                if count >= self.max_images_per_response:
                    break
                components.append(Image.fromURL(image_url))
                count += 1

            for image_path in image_paths:
                if count >= self.max_images_per_response:
                    break
                prepared_path = await self._prepare_image_path(image_path)
                components.append(Image.fromFileSystem(path=prepared_path))
                count += 1

            if components:
                try:
                    await asyncio.wait_for(event.send(event.chain_result(components)), timeout=60.0)
                    if self.status_message_mode == "verbose":
                        await event.send(event.plain_result("✅ 图片发送成功！"))
                except asyncio.TimeoutError:
                    logger.warning(f"用户 {user_id} 的图片发送超时，但可能仍在传输")
                    if self.status_message_mode == "verbose":
                        await event.send(
                            event.plain_result(
                                "⚠️ 图片发送超时，但可能仍在传输中。\n"
                                "如果稍后收到图片，说明发送成功。"
                            )
                        )

            for image_path in image_paths:
                await self._cleanup_image_file(image_path)

        except Exception as e:
            logger.error(f"用户 {user_id} 的图片编辑异常: {e}")
            await event.send(event.plain_result(f"❌ 图片编辑时遇到问题: {str(e)}"))

        finally:
            if user_id in self._processing_tasks and self._processing_tasks[user_id] == task_id:
                del self._processing_tasks[user_id]
                logger.info(f"用户 {user_id} 的任务 {task_id} 已完成")

    @filter.command("修图")
    async def cmd_edit_image(self, event: AstrMessageEvent, *, prompt: str):
        """图片编辑：/修图 <提示词>（需要包含图片）"""
        access_error = await self._check_group_access(event)
        if access_error:
            yield event.plain_result(access_error)
            return

        user_id = str(event.get_sender_id())
        if user_id in self._processing_tasks:
            yield event.plain_result("⚠️ 您已有一个图片编辑任务在进行中，请等待完成后再试。")
            return

        images, extract_error = await self._extract_images_from_message(event)
        if extract_error:
            yield event.plain_result(f"❌ {extract_error}")
            return
        if not images:
            yield event.plain_result("❌ 图片编辑需要您在消息中包含图片或引用包含图片的消息。")
            return

        try:
            task_id = str(uuid.uuid4())[:8]
            self._processing_tasks[user_id] = task_id

            if self.status_message_mode != "silent":
                yield event.plain_result(
                    "🖼️ 正在使用Grok为您编辑图片，请稍候...\n"
                    f"🆔 任务ID: {task_id}\n"
                    "📝 提示：如果显示超时但稍后收到图片，说明发送成功。"
                )

            asyncio.create_task(self._async_edit_image(event, prompt, task_id, prefetched_images=images))

        except Exception as e:
            logger.error(f"图片编辑命令异常: {e}")
            yield event.plain_result(f"❌ 编辑图片时遇到问题: {str(e)}")

    @filter.command("grok测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """测试 Grok API 连接（管理员专用）"""
        if not self._is_admin(event):
            yield event.plain_result("此命令仅限管理员使用")
            return

        try:
            test_results = [Plain("🔍 Grok图片编辑插件测试结果\n" + "=" * 30 + "\n\n")]

            if not self.api_key:
                test_results.append(Plain("❌ API密钥未配置\n"))
            else:
                test_results.append(Plain("✅ API密钥已配置\n"))

            test_results.append(Plain(f"📡 API地址: {self.api_url}\n"))
            test_results.append(Plain(f"🤖 模型ID: {self.model_id}\n"))
            test_results.append(Plain(f"⏱️ 超时时间: {self.timeout_seconds}秒\n"))
            test_results.append(Plain(f"🔄 最大重试: {self.max_retry_attempts}次\n"))
            test_results.append(Plain(f"📁 图片存储目录: {self.images_dir}\n"))
            test_results.append(Plain(f"🖼️ 最多返回图片数: {self.max_images_per_response}\n"))

            if self.enabled:
                test_results.append(Plain("✅ 功能已启用\n"))
            else:
                test_results.append(Plain("❌ 功能已禁用\n"))

            yield event.chain_result(test_results)

        except Exception as e:
            logger.error(f"测试命令异常: {e}")
            yield event.plain_result(f"❌ 测试失败: {str(e)}")

    @filter.command("grok帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """帮助信息"""
        help_text = (
            "🖼️ Grok图片编辑插件帮助\n\n"
            "使用方法：\n"
            "1. 发送一张图片\n"
            "2. 引用该图片发送：/修图 <提示词>\n\n"
            "示例：\n"
            "• /修图 给角色加上墨镜\n"
            "• /修图 改成赛博朋克风格\n"
            "• /修图 把背景换成雪山\n\n"
            "管理员命令：\n"
            "• /grok测试 - 测试API连接\n"
            "• /grok帮助 - 显示此帮助信息\n\n"
            "注意：图片编辑需要一定时间，请耐心等待"
        )
        yield event.plain_result(help_text)

    async def terminate(self):
        self._rate_limit_locks.clear()
        logger.info("Grok图片编辑插件已卸载")
