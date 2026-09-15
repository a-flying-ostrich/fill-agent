"""
命令行交互入口

使用方式：
1. 在 config.py 中填写 API Key
2. 确保 skills/ 目录存在（包含技能 Markdown 文件）
3. pip install -r requirements.txt
4. python main.py

交互命令：
- 直接输入自然语言与 Agent 对话
- 输入 'quit' / 'exit' / '退出' 结束程序
- 输入 'reset' / '重置' 清空对话历史（保留跨会话记忆）
- 输入 'memory' / '记忆' 查看当前记忆索引
- 输入 'skills' / '技能' 查看可用技能列表

Phase 2 新增：
- Agent 启动时自动扫描 skills/ 目录构建技能注册表
- 对话结束后自动提取记忆，存入 .memory/ 目录
- reset 后跨会话记忆保留（Agent 不会完全失忆）
"""

from config import (API_KEY, BASE_URL, MODEL_NAME, DB_PATH, MAX_ITERATIONS, SYSTEM_PROMPT,)
from agent import FillAgent


def main():
    # 检查 API Key 是否已配置
    if API_KEY == "your-api-key-here":
        print("[错误] 请先在 config.py 中设置你的 API Key！")
        return

    # 初始化 Agent
    # Phase 2: FillAgent.__init__ 内部会自动初始化 SkillLoader 和 MemoryManager
    # SYSTEM_PROMPT 是基础提示词，技能目录和记忆索引会在 __init__ 中动态拼接
    agent = FillAgent(
        api_key=API_KEY,
        base_url=BASE_URL,
        model=MODEL_NAME,
        db_path=DB_PATH,
        max_iterations=MAX_ITERATIONS,
        system_prompt=SYSTEM_PROMPT,
    )

    print("=" * 60)
    print("  智能填表助手 (Agent Loop 架构)")
    print("  Phase 2: Memory + Skill Loading + Subagent")
    print("  输入 'quit' / 'exit' / '退出' 结束对话")
    print("  输入 'reset' / '重置' 清空对话历史（保留记忆）")
    print("  输入 'memory' / '记忆' 查看记忆索引")
    print("  输入 'skills' / '技能' 查看可用技能")
    print("=" * 60)

    # 外层 main.py：while True —— 负责【接收用户一句又一句新提问】
    # 内层 agent.py 的 run() 里面：for _ in range(max_iterations) —— 负责【处理当前这一句用户提问，内部反复调用 LLM + 工具，完成这个任务】
    # agent.run(user_input) 只接收 1 次用户输入，但是它函数内部自己会循环很多次：调用 LLM、执行工具、再调用 LLM……，不需要用户再打字，全部是程序自动跑，直到：run 函数执行完毕，才会回到 main 的 while True，才会再次执行input("你: ")等待用户敲下一句
    while True:
        try:
            user_input = input("\n你: ").strip()

            if not user_input:
                continue

            # 退出命令
            if user_input.lower() in ("quit", "exit", "退出"):
                print("再见！")
                break # 跳出死循环

            # 重置对话
            if user_input.lower() in ("reset", "重置"):
                agent.reset()
                continue

            # Phase 2 新增：查看记忆索引
            if user_input.lower() in ("memory", "记忆"):
                memory_index = agent.memory.get_memory_index()
                if memory_index:
                    print(memory_index)
                else:
                    print("[记忆] 暂无记忆。对话结束后会自动提取记忆。")
                continue

            # Phase 2 新增：查看可用技能
            if user_input.lower() in ("skills", "技能"):
                skills = agent.skill_loader.list_skills()
                if skills:
                    print(f"[技能] 已加载 {len(skills)} 个技能：")
                    for name in skills:
                        info = agent.skill_loader.registry[name]
                        print(f"  - {name}: {info['description']}")
                else:
                    print("[技能] 未找到技能文件，请检查 skills/ 目录。")
                continue

            # 运行 Agent Loop
            agent.run(user_input)
            if len(agent.messages) > 2:
                print("\n[提示] 如需开始新任务，请输入 'reset' 清空对话历史。")
        except EOFError:
            print("\n\n再见！")
            break
        except KeyboardInterrupt:
            print("\n\n再见！")
            break
        except Exception as e:
            print(f"\n[错误] {e}")


if __name__ == "__main__":
    main()
