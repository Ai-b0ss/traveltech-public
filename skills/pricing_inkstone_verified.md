> Сохранённый снимок страницы поставщика. Не подтверждает текущие цены, доступность моделей или бесплатность вызова. Действующая конфигурация проекта: [AI_ROUTING](../docs/AI_ROUTING.md).

# 模型列表与计费

不同模型支持的接口、能力、上下文长度和墨点价格可能不同。调用前请以本页清单为准。

## 模型列表

| 模型名称                     | 模型 ID                    | 支持接口                                                      | 输入类型  | 上下文长度 |
| ------------------------ | ------------------------ | --------------------------------------------------------- | ----- | ----: |
| 书生-S2                    | intern-s2                | OpenAI ChatCompletions、Anthropic Messages                 | 文本、图像 |  256K |
| Atria-Dawn-Preview       | Atria-Dawn-Preview       | OpenAI ChatCompletions、OpenAI Response、Anthropic Messages | 文本    |  256K |
| Agents-A1                | Agents-A1                | OpenAI ChatCompletions、Anthropic Messages                 | 文本、图像 |  256K |
| DeepSeek-V4-Flash-0731   | deepseek-v4-flash-0731   | OpenAI ChatCompletions、OpenAI Response、Anthropic Messages | 文本    |    1M |
| DeepSeek-V4-Flash-Vision | deepseek-v4-flash-vision | OpenAI ChatCompletions、OpenAI Response、Anthropic Messages | 文本、图像 |    1M |
| DeepSeek-V4-Pro-0813     | deepseek-v4-pro-0813     | OpenAI ChatCompletions、OpenAI Response、Anthropic Messages | 文本、图像 |    1M |
| GLM-5.3                  | glm-5.3                  | OpenAI ChatCompletions、OpenAI Response、Anthropic Messages | 文本    |    1M |
| Kimi-K2.6                | kimi-k2.6                | OpenAI ChatCompletions、OpenAI Response、Anthropic Messages | 文本、图像 |  256K |
| MiniMax-M3               | minimax-m3               | OpenAI ChatCompletions、OpenAI Response、Anthropic Messages | 文本    |    1M |
| Qwen3.8-27B              | qwen3.8-27b              | OpenAI ChatCompletions、Anthropic Messages                 | 文本、图像 |  256K |


## 墨点价格

墨点为本平台通用的计费单位，TokenPlan 各模型输入输出的价格单位为“墨点 / 百万 Token”。每个模型缓存命中输入、缓存未命中输入和输出价格均不同，详情如下：

| 模型名称                     | 输入（缓存命中） | 输入（缓存未命中） |   输出 | 备注   |
| ------------------------ | -------: | --------: | ---: | ---- |
| 书生-S2                    |        0 |         0 |    0 | 限时免费 |
| Atria-Dawn-Preview       |        0 |         0 |    0 | 限时免费 |
| Agents-A1                |        0 |         0 |    0 | 限时免费 |
| DeepSeek-V4-Flash-0731   |     0.02 |         1 |    4 |      |
| DeepSeek-V4-Flash-Vision |     0.02 |         1 |    4 |      |
| DeepSeek-V4-Pro-0813     |     0.15 |       4.5 | 13.5 |      |
| GLM-5.3                  |        2 |         8 |   28 |      |
| Kimi-K2.6                |      1.1 |       6.5 |   27 |      |
| MiniMax-M3               |     0.42 |       2.1 |  8.4 |      |
| Qwen3.8-27B              |      0.1 |       0.8 |  2.7 |      |

墨点不与 Token 按 1:1 换算。计算公式为：

```text
消耗墨点
= 缓存命中输入 Token ÷ 1,000,000 × 缓存命中输入价格
+ 缓存未命中输入 Token ÷ 1,000,000 × 缓存未命中输入价格
+ 输出 Token ÷ 1,000,000 × 输出价格
```

例如：缓存命中输入 100,000 Token、单价 2；未命中输入 500,000 Token、单价 10；输出 200,000 Token、单价 40，则消耗墨点为：

```text
0.1 × 2 + 0.5 × 10 + 0.2 × 40 = 13.2
```

示例价格仅用于说明计算方式，不代表正式价格。

## 墨点有效期

- 若墨点为一次性发放，则在到期日的 24:00 清空；
- 若墨点为按日发放，则开通后立即生效，有效期至次日 24:00；
- 若墨点为按月发放，则开通后立即生效，但实际计费日为开通后的次日，后续以自然月为周期进行清空。例如：2 月 27 日 15:00 开通，后续在 3 月 28 日/ 4 月 28 日/ x 月 28 日 24:00 清空。

## 结算规则

- 成功请求按照调用时生效的价格和实际 usage 结算。
- 上游无法区分缓存命中与未命中时，输入 Token 按缓存未命中计算。
- 鉴权失败、参数错误和平台自身错误不扣减积分。
- 墨点不足时，平台拒绝新的模型调用。
- 价格可能调整，实际扣减以调用发生时生效的价格为准。

可在“个人中心 → TokenPlan → 用量查询”查看 Token 用量，在“个人中心 → 墨点余额”查看余额和明细。