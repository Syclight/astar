"""Chat Completions compatible transport, with optional SSE streaming."""
import json
import math
import os
from http.client import HTTPException
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener

from astra_core.llm.protocol import ModelError, ModelReply

DEFAULT_MAX_RESPONSE_BYTES = 256_000_000
DEFAULT_MAX_CONTENT_BYTES = 16_000_000


def response_error(reason, message=None, *, stream=False, stage='finish'):
    """Keep protocol metadata only; never persist refusal text or tool arguments."""
    message = message or {}
    known = {'stop', 'length', 'content_filter', 'tool_calls', 'function_call'}
    safe_reason = reason if isinstance(reason, str) and reason in known else ('missing' if reason is None else 'unknown')
    details = {
        'finish_reason': safe_reason, 'stream': stream, 'stage': stage,
        'has_refusal': bool(message.get('refusal')),
        'has_tool_calls': bool(message.get('tool_calls')),
        'has_function_call': bool(message.get('function_call')),
    }
    labels = [f'finish_reason={safe_reason}', f'stage={stage}']
    labels.extend(key for key in ('has_refusal', 'has_tool_calls', 'has_function_call') if details[key])
    return ModelError('模型响应未正常完成：' + '，'.join(labels), details=details)


def check_size(size, limit, field, label):
    if size > limit:
        raise ModelError(f'模型{label}超过限制：已接收 {size:,} 字节，上限 {limit:,} 字节；可配置 {field}。本次响应不完整，未作为有效结果使用')


def parse_stream(value):
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.strip().lower() in {'true', 'false'}:
        return value.strip().lower() == 'true'
    raise ValueError('stream 必须是 true 或 false')


class JsonObjectEnd:
    """Find where the first top-level JSON value closes in streamed text.

    Some servers ignore the requested response format; the model then keeps
    writing (explanations, repeated copies) after a complete JSON reply until
    the output limit. The caller can stop reading as soon as the value closes.
    """
    def __init__(self):
        self.depth = 0
        self.started = self.in_string = self.escape = False

    def feed(self, text):
        """Return the index just after the closing bracket in text, or None."""
        for index, char in enumerate(text):
            if self.in_string:
                if self.escape:
                    self.escape = False
                elif char == '\\':
                    self.escape = True
                elif char == '"':
                    self.in_string = False
            elif char == '"' and self.started:
                self.in_string = True
            elif char in '{[':
                self.started = True
                self.depth += 1
            elif char in '}]' and self.started:
                self.depth -= 1
                if self.depth == 0:
                    return index + 1
        return None


def read_stream(response, on_delta=None, *, max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
                max_content_bytes=DEFAULT_MAX_CONTENT_BYTES, stop_after_json=False):
    """Collect bounded SSE events; never return a partially completed reply.

    With stop_after_json the reply ends when the first JSON object closes;
    closing the connection also tells the server to stop generating.
    """
    parts, data = [], []
    usage, finish = {}, None
    total = 0
    content_bytes = 0
    scanner = JsonObjectEnd() if stop_after_json else None
    while True:
        line = response.readline(max_response_bytes + 1 - total)
        total += len(line)
        check_size(total, max_response_bytes, 'max_response_bytes', '累计传输量')
        if not line:
            raise response_error(finish, stream=True, stage='eof_before_done')
        line = line.decode('utf-8').rstrip('\r\n')
        if line.startswith('data:'):
            data.append(line[5:].removeprefix(' '))
        elif not line and data:
            payload = '\n'.join(data)
            data = []
            if payload == '[DONE]':
                if finish != 'stop':
                    raise response_error(finish, stream=True, stage='done')
                return {'choices': [{'finish_reason': finish,
                                     'message': {'content': ''.join(parts)}}], 'usage': usage}
            chunk = json.loads(payload)
            if 'error' in chunk:
                raise response_error(finish, stream=True, stage='server_error')
            if isinstance(chunk.get('usage'), dict):
                usage = chunk['usage']
            for choice in chunk['choices']:
                if choice.get('index', 0) != 0:
                    continue
                delta = choice.get('delta', {})
                if delta.get('refusal') or delta.get('tool_calls') or delta.get('function_call'):
                    raise response_error(choice.get('finish_reason'), delta, stream=True, stage='delta')
                reasoning = delta.get('reasoning_content') or delta.get('reasoning')
                if on_delta and isinstance(reasoning, str) and reasoning:
                    on_delta('reasoning', reasoning)
                content = delta.get('content')
                if content is not None:
                    if not isinstance(content, str) or finish is not None:
                        raise ModelError('模型服务响应格式无效')
                    end = scanner.feed(content) if scanner is not None else None
                    if end is not None:
                        content = content[:end]
                    content_bytes += len(content.encode('utf-8'))
                    check_size(content_bytes, max_content_bytes, 'max_content_bytes', '正文大小')
                    parts.append(content)
                    if on_delta and content:
                        on_delta('content', content)
                    if end is not None:
                        text = ''.join(parts)
                        start = min(i for i in (text.find('{'), text.find('[')) if i >= 0)
                        return {'choices': [{'finish_reason': 'stop', 'message': {'content': text[start:]}}],
                                'usage': usage, 'stopped_after_json': True}
                reason = choice.get('finish_reason')
                if reason is not None:
                    if reason != 'stop':
                        raise response_error(reason, delta, stream=True)
                    finish = reason


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def reject_legacy_environment(prefix):
    if prefix + 'ENDPOINT' in os.environ:
        raise ValueError(f'{prefix}ENDPOINT 已移除，请改用 {prefix}BASE_URL，填写不含 /chat/completions 的 API 基础地址')


@dataclass(frozen=True)
class ChatModel:
    base_url: str
    model: str
    api_key: str = field(default='', repr=False)
    timeout: float = 60
    max_output_tokens: int = 8192
    token_parameter: str = 'max_completion_tokens'
    stream: bool = False
    response_format: str = 'text'
    reasoning_effort: str = 'auto'
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES

    def __post_init__(self):
        for name in ('max_response_bytes', 'max_content_bytes'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f'{name} 必须是正整数（字节），不能使用零表示无限制')
        if self.reasoning_effort not in {'auto', 'none', 'low', 'medium', 'high', 'max'}:
            raise ValueError('reasoning_effort 必须是 auto、none、low、medium、high 或 max')
        if self.response_format not in {'text', 'json_object', 'json_schema'}:
            raise ValueError('response_format 必须是 text、json_object 或 json_schema')
        if type(self.stream) is not bool:
            raise ValueError('stream 必须是布尔值')
        if not isinstance(self.base_url, str) or self.base_url != self.base_url.strip() or any(c.isspace() for c in self.base_url):
            raise ValueError('模型 base_url 必须是无空白的 HTTP(S) API 基础地址')
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('模型 base_url 必须是无凭证、无查询参数的 HTTP(S) API 基础地址')
        if parsed.path.rstrip('/').endswith('/chat/completions'):
            raise ValueError('base_url 请填写 API 基础地址，不要包含 /chat/completions')
        object.__setattr__(self, 'base_url', self.base_url.rstrip('/'))
        # Limits come from configuration; only reject values that cannot be meant (typos, zero, negatives).
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not (self.timeout > 0 and math.isfinite(self.timeout)):
            raise ValueError(f'模型 timeout 必须是大于 0 的秒数，当前为 {self.timeout!r}')
        if not self.model.strip():
            raise ValueError('模型名称不能为空')
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise ValueError(f'max_output_tokens 必须是正整数，当前为 {self.max_output_tokens!r}')
        if self.token_parameter not in {'max_tokens', 'max_completion_tokens'}:
            raise ValueError('token_parameter 必须是 max_tokens 或 max_completion_tokens')

    @classmethod
    def from_environment(cls):
        reject_legacy_environment('ASTRA_DESIGNER_')
        base_url = os.environ.get('ASTRA_DESIGNER_BASE_URL', '')
        model = os.environ.get('ASTRA_DESIGNER_MODEL', '')
        if not base_url or not model:
            raise ValueError('请配置 ASTRA_DESIGNER_BASE_URL 和 ASTRA_DESIGNER_MODEL')
        return cls(base_url, model, os.environ.get('ASTRA_DESIGNER_API_KEY', ''),
                   float(os.environ.get('ASTRA_DESIGNER_TIMEOUT', '60')),
                   int(os.environ.get('ASTRA_DESIGNER_MAX_OUTPUT_TOKENS', '8192')),
                   os.environ.get('ASTRA_DESIGNER_TOKEN_PARAMETER', 'max_completion_tokens'),
                   parse_stream(os.environ.get('ASTRA_DESIGNER_STREAM', 'false')),
                   os.environ.get('ASTRA_DESIGNER_RESPONSE_FORMAT', 'text'),
                   os.environ.get('ASTRA_DESIGNER_REASONING_EFFORT', 'auto'),
                   int(os.environ.get('ASTRA_DESIGNER_MAX_RESPONSE_BYTES', DEFAULT_MAX_RESPONSE_BYTES)),
                   int(os.environ.get('ASTRA_DESIGNER_MAX_CONTENT_BYTES', DEFAULT_MAX_CONTENT_BYTES)))

    def complete_json(self, messages, schema):
        return self.complete(messages, response_schema=schema)

    def complete_json_with_progress(self, messages, schema, on_progress):
        return self.complete(messages, response_schema=schema, on_delta=on_progress)

    def complete(self, messages, *, on_delta=None, response_schema=None):
        payload = {'model': self.model, 'messages': messages, 'stream': self.stream,
                   self.token_parameter: self.max_output_tokens}
        if self.reasoning_effort != 'auto':
            payload['reasoning_effort'] = self.reasoning_effort
        if self.stream and self.response_format != 'text':
            payload['stream_options'] = {'include_usage': True}
        if self.response_format == 'json_object':
            payload['response_format'] = {'type': 'json_object'}
        elif self.response_format == 'json_schema':
            if response_schema is None:
                raise ValueError('json_schema 模式需要调用方提供响应 Schema')
            from astra_core.llm.contracts import check_schema
            check_schema(response_schema)
            payload['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': 'astra_response', 'schema': response_schema}}
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        request = Request(self.base_url + '/chat/completions', data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
        try:
            with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
                if self.stream:
                    body = read_stream(response, on_delta, max_response_bytes=self.max_response_bytes,
                                       max_content_bytes=self.max_content_bytes,
                                       stop_after_json=response_schema is not None)
                else:
                    raw = response.read(self.max_response_bytes + 1)
                    check_size(len(raw), self.max_response_bytes, 'max_response_bytes', '累计传输量')
                    body = json.loads(raw)
            choice = body['choices'][0]
            message = choice['message']
            if choice.get('finish_reason') != 'stop' or message.get('refusal') or message.get('tool_calls') or message.get('function_call'):
                raise response_error(choice.get('finish_reason'), message, stream=self.stream)
            content = message['content']
            if not isinstance(content, str) or not content.strip():
                raise ModelError('模型未返回文本内容')
            check_size(len(content.encode('utf-8')), self.max_content_bytes, 'max_content_bytes', '正文大小')
            usage = body.get('usage', {})
            usage = {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                     if isinstance(usage, dict) and type(usage.get(k)) is int and usage[k] >= 0}
            return ModelReply(content, usage)
        except HTTPError as exc:
            # Do not persist server error bodies, URLs or Authorization values.
            code = exc.code
            exc.close()
            if code in {400, 422} and self.response_format != 'text':
                raise ModelError(f'模型服务 HTTP {code}；请检查模型与服务是否支持当前 response_format 和响应 Schema；未自动降级或重试') from None
            raise ModelError(f'模型服务 HTTP {code}') from None
        except TimeoutError:
            raise ModelError(f'模型请求等待超过 {self.timeout:g} 秒；服务可能仍在加载模型或生成完整响应') from None
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ModelError(f'模型请求等待超过 {self.timeout:g} 秒；请检查服务负载和模型生成速度') from None
            raise ModelError('无法连接模型服务；请检查接口地址、服务状态及代理设置') from None
        except (OSError, HTTPException):
            raise ModelError('模型连接中断或响应传输失败；请检查服务日志') from None
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ModelError('模型服务响应格式无效') from None
