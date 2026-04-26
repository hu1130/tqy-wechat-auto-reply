# Archive Notes

- Original source path: `/Volumes/Newsmy/Computer/CODE/python+ds+wechat/wechat-AI`
- Local migration path: `/Users/tqy/Code/projects/wechat-AI`
- Migration date: `2026-04-26`
- Migration mode: copied locally first so the external drive keeps the original backup

## Original git history preserved

- `42508c9` Initial commit
- `40707e9` 流式输出
- `a2aa605` 大模型输出
- `6a46916` 测试提交

## Original prototype files

- `demo.py`: local Ollama / DeepSeek output prototype
- `wxdemo.py`: wxauto WeChat message access prototype
- `practice1.py`: unrelated scratch file kept only in git history, removed from the repaired working tree

## Repair goal

Turn the two separate prototypes into one small, maintainable project that:

1. listens to configured WeChat chats via `wxauto`
2. sends recent context to an LLM
3. filters `<think>...</think>` output
4. sends the final reply back to WeChat automatically
