# Grok图片编辑插件

基于 Grok2api 的图片编辑插件，支持根据图片和提示词进行图片编辑。
Grok2api 搭建教程：https://github.com/chenyme/grok2api

## 功能特性

- 🖼️ **图片编辑**：根据图片和文字提示编辑图片
- 🛡️ **权限控制**：支持群组白名单/黑名单和速率限制
- 🔄 **自动重试**：API 请求失败时自动重试
- 📁 **本地存储**：可选保存编辑后的图片
- 🚀 **异步处理**：避免消息超时

## 使用方法

### 基础用法
1. 发送一张图片到群聊或私聊
2. 引用该图片发送命令：`/修图 <提示词>`

### 示例
```
/修图 给角色加上墨镜
/修图 改成赛博朋克风格
/修图 把背景换成雪山
```

## 配置说明

### 必需配置
- **server_url**: Grok API 服务器地址
- **model_id**: 模型 ID（默认：grok-imagine-0.9）
- **api_key**: Grok API 密钥

### 可选配置
- **enabled**: 启用/禁用功能（默认：true）
- **timeout_seconds**: 请求超时时间（默认：120 秒）
- **max_retry_attempts**: 最大重试次数（默认：3 次）
- **prompt_prefix**: 提示词前缀（用于强调基于原图编辑，可留空关闭）
- **status_message_mode**: 提示消息模式（verbose/minimal/silent，默认：minimal）
- **log_input_image_meta**: 记录输入图片元信息（默认：true）
- **auto_compress_enabled**: 首次失败后自动压缩为 JPEG 兜底（默认：true）
- **auto_compress_max_side**: 自动压缩最大边长（默认：1536）
- **auto_compress_quality**: JPEG 压缩质量（默认：85）
- **group_control_mode**: 群组控制模式（off/whitelist/blacklist）
- **group_list**: 群组白名单/黑名单列表
- **rate_limit_enabled**: 启用速率限制（默认：true）
- **rate_limit_window_seconds**: 速率限制窗口（默认：3600 秒）
- **rate_limit_max_calls**: 窗口内最大调用次数（默认：5 次）
- **max_images_per_response**: 单次最多返回图片数量（默认：4）
- **save_image_enabled**: 是否保留生成的图片文件（默认：false）
- **nap_server_address**: NapCat 文件服务器地址（可选）
- **nap_server_port**: NapCat 文件服务器端口（可选）

## 管理员命令

- `/grok测试` - 测试 API 连接状态
- `/grok帮助` - 显示帮助信息

## 技术实现

### API 调用
插件使用 Grok 的 `/v1/chat/completions` 接口，发送包含图片和文字的请求：

```json
{
  "model": "grok-imagine-0.9",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "提示词"},
        {"type": "image_url", "image_url": {"url": "base64图片"}}
      ]
    }
  ]
}
```

### 图片处理
- 支持解析 `data[].url`、`data[].b64_json` 以及 `choices[0].message.content` 中的图片信息
- 若返回 URL，优先以 URL 方式发送
- 若返回 base64，则保存到 AstrBot Data 目录 `data/plugins/astrbot_plugin_grok_image_edit/images/`
- `save_image_enabled=false` 时，发送成功后自动清理缓存
- 如配置 NapCat 文件中转（`nap_server_address/nap_server_port`），会在发送前转存到 NapCat 可访问的路径

## 注意事项

1. **API限制**：Grok 服务可能有访问限制，请确保密钥有效
2. **处理时间**：图片编辑需要一定时间，请耐心等待
3. **网络要求**：需要稳定网络访问 Grok API
4. **容器部署**：若客户端无法访问容器文件路径，请配置 NapCat 文件中转

## 依赖要求

- httpx >= 0.24.0
- Pillow >= 10.0.0（用于图片元信息与自动压缩）

## 版本信息

- 版本：1.0.0
- 作者：ShiHao
- 兼容：AstrBot 插件系统
