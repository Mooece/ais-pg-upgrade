# 来源与致谢

原项目：[DeconstructedCube/aistudio-api](https://github.com/DeconstructedCube/aistudio-api)。本补丁固定对照提交 [`c770aaf3c76a10dbcd6584d033bb76ffa80ccd27`](https://github.com/DeconstructedCube/aistudio-api/commit/c770aaf3c76a10dbcd6584d033bb76ffa80ccd27)。

2026-11-26 适配以该上游源码为底，保留有序响应片段、工具签名规范化、媒体响应、Gemini-4 结束校验、策略拒绝及账户轮换退避，再合入本补丁的 OpenAI 接口、完整回复、缓存和统计功能。字面文本中的 `<thought>` 标签遵循新版上游行为，原样返回。

上一版补丁基于 `a28a6c5b0790c4efa4495de8393631bf1ae814b1`。两版基线不同；旧版代码或旧版已打补丁的安装不能直接套用本版，应先备份数据、回滚旧补丁，再更新并安装指定上游版本。本版不接纳旧基线的文件哈希，避免未覆盖的上游文件仍停留在旧版。

感谢 DeconstructedCube 与原项目贡献者。本补丁的修改文件中包含原项目实现，前端构建还包含其依赖的第三方代码；保留原有版权和许可证声明。原项目 README 将许可声明为 MIT，本仓库不改变该声明，也不代表原项目。

原项目感谢的 WAA 与协议相关项目包括 [Mag1cFall/AIStudio2API](https://github.com/Mag1cFall/AIStudio2API)、[Lianues/aistudio-pg-bridge](https://github.com/Lianues/aistudio-pg-bridge)、[Camoufox](https://github.com/daijro/camoufox) 和 [go-browserforge](https://github.com/brainplusplus/go-browserforge)。完整来源和原项目安装步骤以原项目文档为准。

本仓库独立维护补丁内容，用户应先安装原项目。仓库中的版本控制历史只记录补丁仓库本身。
