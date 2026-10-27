"""Scene-label synonyms, folded at read / filter / write time (M-F2).

Contract (docs/data-contracts.md:868-869): :func:`~ai_coding.memory.note.normalize_scene`
does **not** change -- the fold gate lives here, not in the note.  One table,
shared by the one-off ``scripts/migrate_scenes.py`` and the four read / filter /
write paths, so a second copy can never drift from the first.

This is a spelling merge, not a taxonomy.  Every entry was read in full and named
by what the note says, not by what the model seemed to be thinking of when it
named them: 「工具约束」 and 「执行约束」 share the word 约束 and nothing else --
the first is what the tools may do, the second is what a run may do -- and a
fuzzy matcher that reads the shared word would merge them into one, after which
``scene=`` silently answers a question nobody asked.
"""

from __future__ import annotations

from collections.abc import Mapping

from ai_coding.memory.note import normalize_scene

__all__ = ["SCENE_SYNONYMS", "fold_scene"]

#: old label -> the label that replaces it.  A value that is also a key would
#: need a second pass, so the fold would not be done when it claims to be; the
#: table is therefore idempotent (``tests/test_memory_scenes.py`` pins it).
SCENE_SYNONYMS: Mapping[str, str] = {
    "记忆模块": "记忆",
    "记忆系统": "记忆",
    "记忆配置": "记忆",
    "项目概况": "项目",
    "项目环境": "项目",
    "项目结构": "项目",
    "环境依赖": "环境",
    "环境信息": "环境",
    "环境版本": "环境",
    "环境约束": "环境",
    "环境配置": "环境",
    "文档规范": "文档",
    "文档组织": "文档",
    "架构演进": "架构",
    "架构约束": "架构",
    "配置管理": "配置",
    "配置约定": "配置",
    "配置文件": "配置",
    "MCP架构": "MCP",
    "MCP设置": "MCP",
    "开发命令": "开发流程",
    "执行约束": "开发流程",
    "分支管理": "开发流程",
    "工具环境": "工具",
    "工具约束": "工具",
    "UI状态栏": "界面",
    "UI组件": "界面",
    "酒店管理系统": "工具",
    "路由检查": "依赖",
    "测试质量": "测试",
    "单元测试": "测试",
    "模型配置": "技术",
    "权限控制": "权限",
    "db": "数据库",
    "api": "接口",
}


def fold_scene(value: str) -> str:
    """The synonym of ``value`` under :func:`normalize_scene`.

    ``normalize_scene`` itself is unchanged (contract).  The table's values are
    never keys, so a second fold is a fixed point -- ``fold_scene(fold_scene(x))
    == fold_scene(x)``.
    """
    normalized = normalize_scene(value)
    return SCENE_SYNONYMS.get(normalized, normalized)
