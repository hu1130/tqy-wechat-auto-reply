import requests
import json
import re


def func(question, model='deepseek-r1:14b', API_URL="http://localhost:11434/api/generate"):
    headers = {"Content-Type": "application/json"}
    data = {"model": model, "prompt": question, "stream": True}

    # 状态变量，用于跟踪过滤过程
    in_tag = False  # 标记是否在"aaa"和"bbb"之间
    buffer = ""  # 存储尚未处理的字符
    tag_closed = False  # 标记首个标签对是否已经关闭

    response = requests.post(API_URL, headers=headers, json=data, stream=True)

    for line in response.iter_lines():
        if line:
            json_data = json.loads(line.decode("utf-8"))
            #print(json_data)
            char = json_data.get("response", "")

            if tag_closed:
                # 如果第一个标签对已经关闭，直接输出后续所有内容
                print(char, end="", flush=True)
                continue

            buffer += char  # 将字符存入缓冲区
            print(char, end="", flush=True)  # 即时输出字符

            # 如果我们在标签内
            if in_tag:
                # 检查是否遇到bbb结束标签
                if "</think>" in buffer:
                    # 移除缓冲区中从aaa到bbb的所有内容
                    buffer = re.sub(r'<think>.*?</think>', '', buffer, 1, re.DOTALL)
                    # 确保不会再有标签内容被删除
                    tag_closed = True
                    # 重置标签状态
                    in_tag = False
            else:
                # 检查是否遇到aaa开始标签
                if "<think>" in buffer:
                    in_tag = True
                    # 立即移除已打印的aaa开始标签内容
                    print('\b' * len(buffer[buffer.find("<think>"):]), end='', flush=True)

func('hello', model='deepseek-r1:14b')