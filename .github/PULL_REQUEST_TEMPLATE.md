<!-- 提交前请确认已阅读 CONTRIBUTING.md -->

## 改了什么

<!-- 简要说明这个 PR 做了什么 -->

## 为什么改

<!-- 解决什么问题 / 对应哪个 Issue（如 Fixes #12） -->

## 怎么验证的

<!-- 贴出测试命令与结果 -->

```
pytest
python tests/e2e/run_e2e.py
```

## 检查清单

- [ ] 从 `master` 开的分支，一个 PR 只做一件事
- [ ] `pytest` 全部通过
- [ ] `python tests/e2e/run_e2e.py` 全部通过
- [ ] 改动了核心逻辑 → 已补充对应测试
- [ ] 改动了用户可见行为 → 已同步更新 README.md 与 CHANGELOG.md
- [ ] 未提交任何 API Key、密钥文件或本地绝对路径
- [ ] 代码符合项目规范（类型注解、日志、无 `print`）

## 补充说明

<!-- 需要 reviewer 特别注意的地方、已知的取舍、后续计划等 -->
