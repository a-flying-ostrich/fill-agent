"""
LLM 客户端封装
通过 OpenAI 兼容接口调用 LLM（当前：硅基流动 GLM-4.5-Air）。

核心职责：
- 封装 API 调用细节，对外提供统一的 chat() 方法
- 将 OpenAI 响应对象转换为普通字典，方便 Agent Loop 处理
- 这个模块的唯一职责是把 OpenAI API 的调用细节封装起来，对外只暴露一个 chat() 方法。它的存在让 Agent Loop 不需要关心网络请求、响应解析等细节。
"""

import time
from typing import List, Dict, Optional
from openai import OpenAI

class LLMClient:
    """LLM 客户端：封装 OpenAI 兼容 API 调用"""

    def __init__(self, api_key: str, base_url: str, model: str):
        """
        初始化 LLM 客户端。

        Args:
            api_key:  API Key（硅基流动 / 本地 vLLM 均可）
            base_url: OpenAI 兼容接口地址
            model:    模型名称（如 zai-org/GLM-4.5-Air）
        """
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url) # OpenAI SDK 的客户端初始化，自动处理 http 请求、鉴权、重试底层逻辑

    def chat(self, messages: List[Dict], tools: Optional[List[Dict]] = None, tool_choice: str = "auto") -> Dict: # List[Dict]是元素是字典的列表，代表标准 OpenAI 消息格式，tools 是可选的工具 JSON Schema 列表
        """
        发送对话请求，返回响应。只干一件事：发请求、拿模型返回结果，把 Pydantic 对象转成干净字典返回给上层，仅此而已。

        Args:
            messages: 对话历史（标准 OpenAI 消息格式）
            tools:    可用工具的 JSON Schema 列表（可选）

        Returns:
            {
                "content": str | None,       # LLM 的文本回复（可能有也可能没有）
                "tool_calls": list | None,   # 工具调用列表（如果 LLM 决定调用工具）
                "finish_reason": str         # 结束原因："stop" / "tool_calls" / "length"
            }
        OpenAI 兼容协议里，finish_reason 常见就三种：
        | 值 | 含义 | 状态 |
        | --- | --- | --- |
        | stop | 模型正常说完了，自然结束 | 正常 |
        | tool_calls | 模型正常输出了完整的工具调用 | 正常 |
        | length | 输出达到了max_tokens上限，被强制截断，内容不完整 | 异常终止 |    

        Raises:
            Exception: API 调用失败时抛出，由 Agent Loop 捕获处理
        """
        kwargs: Dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 2048,  # 限制输出长度，防止content过长挤掉tool_calls
            "frequency_penalty": 0.3,  # 抑制重复，Qwen2.5 长上下文下容易重复退化
        } # : Dict是类型注解 (Type Hint)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        # ===== 429 限流自动重试 =====
        # 硅基流动有 TPM（每分钟 token 数）限制，多轮工具调用后容易触发
        # 遇到 429 时等待后自动重试，最多重试 3 次
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(**kwargs) 
                break
            except Exception as e:
                error_str = str(e)
                # 检测是否为 429 限流错误
                if "429" in error_str or "rate" in error_str.lower() or "tpm" in error_str.lower():
                    if attempt < max_retries - 1:
                        wait = (attempt + 1) * 5  # 第1次等5秒，第2次等10秒，第3次等15秒
                        print(f"\n[限流] API 限流，等待 {wait} 秒后重试（第 {attempt + 1}/{max_retries} 次）...")
                        time.sleep(wait)
                        continue
                    else:
                        print(f"\n[限流] 已重试 {max_retries} 次，仍然限流，放弃。")
                        raise
                # 非 429 错误，直接抛出
                raise

        # kwargs语法，把字典解包，把字典里每一个 key‑value，拆成函数的命名参数传入，等价于：client.chat.completions.create(model=self.model,messages=messages,tools=tools)，函数调用语法参数名不能带引号
        # self.client是self.client = OpenAI(api_key=api_key, base_url=base_url)
        # OpenAI() 相当于造一台 "对讲机"，client = 这台对讲机，response = 服务器传回的完整回复数据包
        '''
        self.client.chat.completions.create（）向大模型发起请求:
        调用 OpenAI 兼容接口的参数解释：
        model=self.model：指定使用模型类型
        messages=messages：把全部对话历史发给模型（最重要！模型记得之前所有对话、命令结果）
        tools=tools：把工具说明书一起传给模型
        调用结束，response 保存大模型返回的完整数据包,response就是一个Pydantic对象。
        '''

        # 防御性检查：API 可能返回空 choices（对话过长、限流等）
        if not response.choices:
            raise Exception(f"API 返回空 choices 列表，可能是上下文过长。response: {response}")

        choice = response.choices[0]
        msg = choice.message
        '''
        先模拟服务器返回的原始 JSON（标准格式）response
        {
        "id": "xxx",
        "object": "chat.completion",
        "choices": [
            {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "回答文本",
                "tool_calls": null    注：openai官方规范：建议二者content,tool_calls只留一个，互斥。但是现实推理时开源模型可以同时输出 content + tool_calls，合法
            },
            "finish_reason": "stop"
            }
        ]
        }
        choices 是数组，choices[0] = 第一条候选结果 → 存入变量choice
        在 JSON 里，这条结果里面有一个 key 叫做 message
        SDK 把 JSON 映射成对象后：
        JSON 的"message" → 对象属性 .message

        底层原始 JSON，如果当成Python 字典，写法是：
        choice_dict["message"]
        但是！openai SDK(别人封装好的代码工具箱,打包给你直接调用) 收到服务器返回的 JSON 之后，不会直接给你原生字典。
        SDK 把 JSON 解析完，封装成了 Pydantic 模型对象。
        访问对象里面的字段，语法就变成了：
        choice.message

        原始 JSON 结构 → openai SDK 解析后变成对象访问方式:
        json["choices"][0]["message"]["content"]
        ↓ SDK封装简化 ↓
        response.choices[0].message.content

        vLLM/OpenAI 接口返回格式：choices 是候选回答列表，一般只返回 1 条结果。
        response.choices[0]：拿到第一条模型回复
        msg：模型消息对象，里面包含两种可能：
        自然语言文本 msg.content
        工具调用指令 msg.tool_calls（如果用到了会显示指令，如果没有用到说明大模型回答认为不需要再bash了，退出循环）
        '''

        # 将 OpenAI 响应对象转换为普通字典
        # tool_calls 是自定义对象，需要手动提取字段
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        # tool_call.function.arguments：SDK已完成HTTP响应顶层JSON解析，此处拿到的是Python字符串，不是字典；字符串内部才是参数的JSON‑object文本
                        # "arguments"对应的值是调用这个工具所需要的全部入参，以 JSON 文本的形式打包成一个字符串，llm提供的工具调用参数是一个 JSON 字符串，llm 解析后传给 agent loop，agent loop 再把这个 JSON 字符串解析成字典，传给工具函数
                        "arguments": tool_call.function.arguments, 
                    },
                }
                for tool_call in msg.tool_calls
            ] 
        # for tool_call in msg.tool_calls列表推导式，tool_calls就是一个字典列表，手动构建字典供 Agent Loop 使用，方便后续判断调用哪个工具
        '''
        tool_call Padantic对象结构对应底层 JSON：
        {
        "id": "tc-xxx",
        "function": {
            "name": "bash",
            "arguments": "{\"command\":\"ls *.py\"}"  # arguments 是 json-string，不是 json-object
        }
        }
        tool_call.function：又是一个子对象
        .name：取出工具名字 "bash"
        存入变量 func_name，用来判断调用哪个工具。
        '''

        return {
            "content": msg.content,
            "tool_calls": tool_calls,
            "finish_reason": choice.finish_reason,
        }
    '''
    JSON 里的 6 种类型，和 json.loads 输出的 Python 类型，是一一映射，但不是全都变成 dict/list。
    | JSON 类型 | json.loads () 之后得到的 Python 类型 |
    | --- | --- | --- |
    | JSON object `{...}` | Python `dict` 字典 |
    | JSON array `[...]` | Python `list` 列表 |
    | JSON string `"hello"` | Python `str` 字符串 |
    | JSON number `123` | Python `int` / `float` |
    | JSON boolean `true/false` | Python `True` / `False` |
    | JSON `null` | Python `None` |
    ⚠️只有 JSON 文本最外层是`{}`或者`[]`的时候，loads 才输出 dict/list。
    如果 JSON 文本最外层本身就是一个 JSON 字符串，那 loads 之后得到的就是 Python str。
    '''