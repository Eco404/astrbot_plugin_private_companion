# 边界与情感反馈

这是一个独立的 AstrBot 插件，目录必须直接放在 AstrBot 的 `data/plugins/astrbot_plugin_nene_boundary` 下才会被扫描加载。它不能作为 `astrbot_plugin_private_companion` 的子目录直接启用。

启用前请确认已安装并启用私聊陪伴插件。边界插件通过 AstrBot 已注册插件实例联动；不要手工指定其他机器上的数据文件或数据库路径。

本地测试示例：

```powershell
$env:ASTRBOT_BACKEND_APP = 'C:\\path\\to\\AstrBot\\backend\\app'
python -m pytest -q tests/test_nene_boundary.py
```
