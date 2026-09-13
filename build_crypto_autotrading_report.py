from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from xml.sax.saxutils import escape


OUT = Path("加密货币自动交易资料调研报告.docx")


NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def esc(value: str) -> str:
    return escape(str(value), {'"': "&quot;", "'": "&apos;"})


def run(text: str, bold: bool = False, italic: bool = False, size: int = 22, color: str = "000000", font: str = "Noto Sans SC") -> str:
    props = [f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:eastAsia="{font}"/>', f'<w:sz w:val="{size}"/>', f'<w:szCs w:val="{size}"/>', f'<w:color w:val="{color}"/>']
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    return f'<w:r><w:rPr>{"".join(props)}</w:rPr><w:t xml:space="preserve">{esc(text)}</w:t></w:r>'


def paragraph(text: str = "", style: str = "Normal", align: str | None = None, before: int = 0, after: int = 120, line: int = 300, keep: bool = False) -> str:
    ppr = [f'<w:pStyle w:val="{style}"/>', f'<w:spacing w:before="{before}" w:after="{after}" w:line="{line}" w:lineRule="auto"/>']
    if align:
        ppr.append(f'<w:jc w:val="{align}"/>')
    if keep:
        ppr.append("<w:keepNext/>")
    style_sizes = {"Title": 36, "Subtitle": 24, "Heading1": 28, "Heading2": 24, "Heading3": 22, "Source": 17}
    content = run(text, size=style_sizes.get(style, 22))
    return f'<w:p><w:pPr>{"".join(ppr)}</w:pPr>{content}</w:p>'


def rich_paragraph(parts: list[tuple[str, dict]], style: str = "Normal", align: str | None = None, before: int = 0, after: int = 120, line: int = 300) -> str:
    ppr = [f'<w:pStyle w:val="{style}"/>', f'<w:spacing w:before="{before}" w:after="{after}" w:line="{line}" w:lineRule="auto"/>']
    if align:
        ppr.append(f'<w:jc w:val="{align}"/>')
    body = "".join(run(text, **kwargs) for text, kwargs in parts)
    return f'<w:p><w:pPr>{"".join(ppr)}</w:pPr>{body}</w:p>'


def heading(text: str, level: int = 1) -> str:
    style = f"Heading{level}"
    return paragraph(text, style=style, before=240 if level == 1 else 160, after=100, line=280, keep=True)


def bullet(text: str, level: int = 0) -> str:
    indent = 360 + level * 360
    return f'<w:p><w:pPr><w:pStyle w:val="ListBullet"/><w:ind w:left="{indent}" w:hanging="180"/><w:spacing w:after="70" w:line="280" w:lineRule="auto"/></w:pPr>{run(text)}</w:p>'


def shade(fill: str) -> str:
    return f'<w:shd w:fill="{fill}" w:val="clear"/>'


def cell(text: str, width: int, header: bool = False, align: str = "left", fill: str | None = None) -> str:
    tcpr = [f'<w:tcW w:w="{width}" w:type="dxa"/>', f'<w:tcMar><w:top w:w="100" w:type="dxa"/><w:left w:w="110" w:type="dxa"/><w:bottom w:w="100" w:type="dxa"/><w:right w:w="110" w:type="dxa"/></w:tcMar>', '<w:vAlign w:val="center"/>']
    if fill:
        tcpr.append(shade(fill))
    # light gray borders on every cell
    tcpr.append('<w:tcBorders><w:top w:val="single" w:sz="4" w:color="D9D9D9"/><w:left w:val="single" w:sz="4" w:color="D9D9D9"/><w:bottom w:val="single" w:sz="4" w:color="D9D9D9"/><w:right w:val="single" w:sz="4" w:color="D9D9D9"/></w:tcBorders>')
    color = "FFFFFF" if header else "000000"
    return f'<w:tc><w:tcPr>{"".join(tcpr)}</w:tcPr>{rich_paragraph([(text, {"bold": header, "size": 18 if header else 18, "color": color})], align=align, after=0, line=240)}</w:tc>'


def table(headers: list[str], rows: list[list[str]], widths: list[int]) -> str:
    header_fill = "17365D"
    body = [f'<w:tblPr><w:tblW w:w="{sum(widths)}" w:type="dxa"/><w:tblLayout w:type="fixed"/><w:tblBorders><w:top w:val="single" w:sz="4" w:color="D9D9D9"/><w:left w:val="single" w:sz="4" w:color="D9D9D9"/><w:bottom w:val="single" w:sz="4" w:color="D9D9D9"/><w:right w:val="single" w:sz="4" w:color="D9D9D9"/><w:insideH w:val="single" w:sz="4" w:color="D9D9D9"/><w:insideV w:val="single" w:sz="4" w:color="D9D9D9"/></w:tblBorders></w:tblPr>']
    body.append('<w:tblGrid>' + ''.join(f'<w:gridCol w:w="{w}"/>' for w in widths) + '</w:tblGrid>')
    body.append('<w:tr><w:trPr><w:tblHeader/></w:trPr>' + ''.join(cell(h, w, header=True, fill=header_fill) for h, w in zip(headers, widths)) + '</w:tr>')
    for i, row in enumerate(rows):
        fill = "F2F6FA" if i % 2 else "FFFFFF"
        body.append('<w:tr><w:trPr><w:cantSplit/></w:trPr>' + ''.join(cell(v, w, fill=fill) for v, w in zip(row, widths)) + '</w:tr>')
    return '<w:tbl>' + ''.join(body) + '</w:tbl>'


def page_break() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def hyperlink(url: str) -> str:
    # Keep URLs readable and copyable without adding relationship complexity.
    return paragraph(url, style="Source")


def styles_xml() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{NS_W}">
  <w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Noto Sans SC" w:hAnsi="Noto Sans SC" w:eastAsia="Noto Sans SC"/><w:sz w:val="22"/><w:szCs w:val="22"/><w:color w:val="000000"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Noto Sans SC" w:hAnsi="Noto Sans SC" w:eastAsia="Noto Sans SC"/><w:sz w:val="22"/><w:szCs w:val="22"/><w:color w:val="000000"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Subtitle"/><w:uiPriority w:val="1"/><w:qFormat/><w:pPr><w:jc w:val="center"/><w:spacing w:before="0" w:after="160" w:line="300"/></w:pPr><w:rPr><w:rFonts w:ascii="Noto Sans SC" w:hAnsi="Noto Sans SC" w:eastAsia="Noto Sans SC"/><w:b/><w:sz w:val="36"/><w:szCs w:val="36"/><w:color w:val="000000"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:pPr><w:jc w:val="center"/><w:spacing w:after="260"/></w:pPr><w:rPr><w:rFonts w:ascii="Noto Sans SC" w:hAnsi="Noto Sans SC" w:eastAsia="Noto Sans SC"/><w:sz w:val="24"/><w:szCs w:val="24"/><w:color w:val="404040"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="300" w:after="120" w:line="280"/></w:pPr><w:rPr><w:rFonts w:ascii="Noto Sans SC" w:hAnsi="Noto Sans SC" w:eastAsia="Noto Sans SC"/><w:b/><w:sz w:val="28"/><w:szCs w:val="28"/><w:color w:val="000000"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="220" w:after="80" w:line="270"/></w:pPr><w:rPr><w:rFonts w:ascii="Noto Sans SC" w:hAnsi="Noto Sans SC" w:eastAsia="Noto Sans SC"/><w:b/><w:sz w:val="24"/><w:szCs w:val="24"/><w:color w:val="000000"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="150" w:after="60"/></w:pPr><w:rPr><w:rFonts w:ascii="Noto Sans SC" w:hAnsi="Noto Sans SC" w:eastAsia="Noto Sans SC"/><w:b/><w:sz w:val="22"/><w:szCs w:val="22"/><w:color w:val="000000"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Source"><w:name w:val="Source"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="45" w:line="220"/></w:pPr><w:rPr><w:rFonts w:ascii="Noto Sans SC" w:hAnsi="Noto Sans SC" w:eastAsia="Noto Sans SC"/><w:sz w:val="17"/><w:szCs w:val="17"/><w:color w:val="404040"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="70" w:line="280"/></w:pPr></w:style>
</w:styles>'''


def document_xml(body: str) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{NS_W}" xmlns:r="{NS_R}"><w:body>{body}
<w:sectPr><w:footerReference w:type="default" r:id="rId2"/><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080" w:header="500" w:footer="500" w:gutter="0"/><w:cols w:num="1"/><w:docGrid w:linePitch="360"/></w:sectPr></w:body></w:document>'''


def footer_xml() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr xmlns:w="{NS_W}"><w:p><w:pPr><w:jc w:val="center"/></w:pPr>{run("加密货币自动交易资料调研报告  |  2026年9月8日  |  第 ", size=16, color="666666")}<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>{run(" 页", size=16, color="666666")}</w:p></w:ftr>'''


def make_body() -> str:
    parts: list[str] = []
    parts.append(paragraph("加密货币自动交易资料调研报告", style="Title", align="center", after=120))
    parts.append(paragraph("开源项目、热门策略、交易所 API 与工程落地", style="Subtitle", align="center", after=260))
    parts.append(paragraph("研究日期：2026年9月8日", align="center", after=40))
    parts.append(paragraph("适用对象：准备搭建或迭代加密货币自动交易系统的产品、量化和工程团队", align="center", after=260))
    parts.append(paragraph("本报告基于公开官方文档、项目仓库和交易所 API 文档整理，重点回答四个问题：可以选哪些开源基础设施，常见策略如何实现，交易所 API 如何接入，以及如何把回测、风控和实盘执行连成可验证的工程系统。报告不构成投资建议，也不对任何策略的盈利能力作保证。", after=180, line=320))
    parts.append(table(["结论", "建议"], [
        ["优先级", "先把数据、风控、订单状态机和审计做成确定性底座，再叠加策略与模型。"],
        ["开源选型", "单策略研究可从 Freqtrade 或 Jesse 入手；做做市、套利和多连接器时重点看 Hummingbot；需要事件驱动、低延迟和多资产统一抽象时看 NautilusTrader 或 LEAN。"],
        ["API 方案", "REST 用于下单、查询和补偿；WebSocket 用于行情、订单和账户事件；任何网络超时都必须通过 clientOrderId、订单查询和幂等逻辑恢复。"],
        ["验证方法", "回测必须纳入手续费、滑点、资金费率、延迟、最小下单量和强平规则，并用 walk-forward、纸面盘和测试网逐级验证。"],
    ], [1500, 8100]))
    parts.append(page_break())

    parts.append(heading("一 研究范围与术语", 1))
    parts.append(paragraph("自动交易系统不是单一的策略脚本，而是由市场数据、信号生成、组合与风险、订单执行、状态持久化和监控告警组成的闭环。策略产生的是意图，风险层决定意图是否可执行，执行层负责把批准后的订单变成可追踪的交易状态。"))
    parts.append(heading("关键术语", 2))
    parts.append(table(["术语", "含义", "工程关注点"], [
        ["Spot / Futures", "现货与合约市场。合约通常涉及杠杆、保证金、资金费率和强平。", "账户模式、保证金模式、仓位模式、标记价格与强平价格必须进入风控。"],
        ["Maker / Taker", "挂单提供流动性或主动吃单。", "费率、成交概率、排队位置和撤单频率会改变策略的真实收益。"],
        ["Dry-run / Paper", "用实时行情模拟下单，不接触真实资金。", "要模拟延迟、成交概率、部分成交和拒单，不能只记录信号。"],
        ["Client Order ID", "由客户端生成的订单标识。", "用于幂等、超时恢复、审计和跨 REST/WebSocket 的订单关联。"],
        ["Walk-forward", "滚动训练或参数选择，再在后续未见数据上验证。", "用于发现过拟合，不能替代真实交易环境验证。"],
    ], [1900, 4700, 3000]))

    parts.append(heading("二 开源项目与基础设施", 1))
    parts.append(paragraph("下表按定位而不是按 GitHub 热度排序。项目功能、连接器数量、许可证和活跃度会变化，正式选型前应重新核对仓库和文档。"))
    parts.append(table(["项目", "定位", "主要能力", "适合场景"], [
        ["Freqtrade", "Python 策略机器人", "现货与合约策略、回测、Hyperopt、dry-run、Web API、策略模板和 FreqAI 扩展。", "快速验证技术指标、规则策略和中低频自动交易。[S1][S2]"],
        ["Hummingbot", "连接器与做市框架", "CEX/DEX 连接器、纯做市、跨交易所做市、套利、网格和策略脚本。", "做市、跨市场价差、流动性提供和多连接器交易。[S3][S4]"],
        ["CCXT", "统一交易所 API 库", "统一 REST 市场数据、账户和订单接口；通过统一参数处理不同交易所差异。", "快速接入多个中心化交易所，适合作为适配层或原型底座。[S5]"],
        ["Jesse", "Python 研究与交易框架", "策略开发、回测、指标、参数优化和实盘交易工作流。", "偏研究型团队和希望使用 Python 统一开发体验的团队。[S6]"],
        ["NautilusTrader", "事件驱动交易平台", "事件驱动架构、回测与实盘统一、订单簿数据、交易适配器和高性能组件。", "需要统一回测/实盘语义、低延迟或多资产扩展的团队。[S7]"],
        ["LEAN", "通用算法交易引擎", "多资产数据、回测、优化、实盘部署和算法模块化。", "希望使用成熟事件引擎和多资产研究框架的团队。[S8]"],
        ["vectorbt", "向量化研究工具", "基于 NumPy、pandas 和 Numba 的大规模参数扫描、组合回测和可视化。", "快速研究因子、参数空间和组合构造，不直接替代实盘执行系统。[S9]"],
        ["backtrader", "Python 回测框架", "数据源、指标、策略、经纪商抽象和分析器。", "已有 Python 研究代码、需要传统事件回测接口的团队。[S10]"],
    ], [1800, 2100, 4000, 2300]))
    parts.append(heading("项目选型判断", 2))
    parts.append(bullet("如果核心目标是尽快完成策略假设验证，优先考虑 Freqtrade、Jesse 或 vectorbt。"))
    parts.append(bullet("如果核心目标是持续报价、库存管理和套利，优先研究 Hummingbot，并重点评估连接器质量、订单簿一致性和库存风险。"))
    parts.append(bullet("如果团队需要把回测、模拟盘和实盘使用同一套事件语义，NautilusTrader 或 LEAN 的架构更值得深入评估。"))
    parts.append(bullet("CCXT 适合作为多交易所接入的加速器，但不能掩盖交易所之间在订单类型、仓位、保证金、限频和错误码上的本质差异。生产系统仍应保留交易所专用适配器。"))

    parts.append(heading("三 热门交易策略与实现要点", 1))
    parts.append(paragraph("策略的“热门”不等于稳定盈利。以下分类按公开框架常见用法和市场微观结构整理，重点是输入、信号、执行和失效条件。任何策略都应先在含成本的历史数据上验证，再经过纸面盘、测试网和极小资金实盘。"))
    parts.append(table(["策略", "典型信号", "技术实现", "主要风险"], [
        ["趋势跟随 / 动量", "均线、ADX、突破、收益率排序、波动率调整后的方向信号。", "多周期 K 线、趋势过滤、ATR 止损、仓位按波动率缩放、移动止盈。", "震荡市场连续止损；参数和换仓频率容易过拟合。"],
        ["均值回归", "价格偏离均线、布林带、z-score、短期反转。", "标准化残差、半衰期估计、价差或组合回归、超时退出。", "趋势启动时会逆势加仓；极端行情下均值可能长期不回归。"],
        ["突破 / 波动率扩张", "高低点突破、Donchian、ATR 突破、波动率压缩后扩张。", "突破确认、成交量或盘口过滤、滑点上限、保护性止损。", "假突破、跳空和流动性不足导致成交价格恶化。"],
        ["网格", "在价格区间内分层挂单，价格每移动一格触发买卖。", "网格间距、库存上限、动态中心价、止损和趋势熔断。", "单边趋势中库存越积越大；资金占用和手续费敏感。"],
        ["做市", "围绕中间价双边报价，按库存和波动率偏移报价。", "订单簿、microprice、库存偏斜、报价半径、撤单和排队建模。", "逆向选择、延迟、库存风险和突发波动。"],
        ["跨交易所套利", "同一资产在不同市场的价差超过成本和风险阈值。", "同步行情、资金与持仓预置、双腿执行、失败腿补偿。", "转账和提现延迟、流动性、腿风险、API 限频和账户冻结。"],
        ["资金费率 / 基差", "现货与永续合约的资金费率或基差达到阈值。", "现货多头与合约空头对冲，持仓和资金费率预测，定时再平衡。", "资金费率反转、基差收敛不确定、保证金和借贷成本。"],
        ["因子与横截面排序", "动量、波动率、成交量、资金费率、持仓量、相关性等因子排序。", "截面 z-score、IC、组合约束、行业或资产相关性控制。", "因子衰减、拥挤交易、数据泄漏和组合换手成本。"],
        ["机器学习 / 强化学习", "分类、回归、序列模型或策略代理输出概率、分数或动作。", "特征版本、训练/验证隔离、概率校准、模型漂移和回退策略。", "样本外失效、数据泄漏、不可解释和训练环境与实盘不一致。"],
    ], [1700, 3100, 4100, 2300]))
    parts.append(heading("策略设计的通用公式", 2))
    parts.append(paragraph("仓位大小应由账户权益、单笔风险和止损距离共同决定，而不是只由信号强弱决定。一个常见的确定性形式是："))
    parts.append(paragraph("仓位数量 = 账户权益 × 单笔风险比例 ÷ |入场价 - 止损价|", align="center", before=60, after=120))
    parts.append(paragraph("实际系统还要继续应用最小下单量、价格精度、名义价值、保证金、最大持仓、组合相关性、日内损失上限和断路条件。信号层可以提出入场方向，但不能绕过这些约束。"))
    parts.append(heading("执行算法与策略的区别", 2))
    parts.append(paragraph("TWAP、VWAP、冰山单、拆单和限价追单主要解决“如何成交”，不等于“何时交易”。它们通常是执行层组件，可以被趋势、套利或再平衡策略复用。把 alpha 信号和执行逻辑分开，便于独立测试成交成本和异常恢复。"))

    parts.append(heading("四 交易所 API 与通信方案", 1))
    parts.append(paragraph("生产接入通常采用 REST + WebSocket 的组合。REST 适合鉴权、下单、查询快照和补偿；WebSocket 适合实时行情、订单回报和账户事件。任何 WebSocket 连接都可能断开、乱序或丢消息，因此必须定期用 REST 快照校正本地状态。"))
    parts.append(table(["交易所", "主要 API", "认证与接入要点", "适合用途"], [
        ["Binance", "Spot、USD-M Futures REST；WebSocket streams；用户数据流。", "API Key/Secret 签名；需处理时间戳、recvWindow、权重限频、交易规则和 listenKey/用户流生命周期。[S11][S12]", "流动性较高的现货和合约策略，适合做单交易所 MVP。"],
        ["Coinbase Advanced", "REST 与 WebSocket 的 Advanced Trade API。", "JWT/API Key 鉴权；实时行情与订单事件分通道；需验证产品、地区和账户权限。[S13][S14]", "合规要求较高或偏现货的美国市场接入。"],
        ["OKX", "V5 REST 与 WebSocket，覆盖现货、永续、期货和期权。", "API Key、Secret、Passphrase；需处理 instId、tdMode、posSide、账户模式和频道订阅。[S15]", "多品种、多账户模式和衍生品策略。"],
        ["Bybit", "V5 REST 与 WebSocket，统一多产品接口。", "签名鉴权、时间戳和 recv_window；需区分 category、账户模式、订单类型和私有频道。[S16]", "永续、期货和现货的统一接入。"],
        ["Kraken", "Spot REST 与 WebSocket API。", "私有接口签名、nonce、账户权限和订单回报；需关注产品规则与限频。[S17]", "偏现货、重视账户安全和多市场覆盖的场景。"],
    ], [1500, 2800, 4500, 2400]))
    parts.append(heading("API 接入必须实现的工程能力", 2))
    for text in [
        "密钥隔离：API Key 只放在服务器密钥存储或受限环境变量中，不进入浏览器、数据库业务表、日志和错误追踪。",
        "权限最小化：优先关闭提现权限；按策略拆分子账户或 API Key；对 IP 白名单、签名算法和权限变更做审计。",
        "连接恢复：WebSocket 断线重连要有退避、订阅恢复、序列号校验和 REST 快照重建。",
        "订单幂等：每次提交携带可追踪的 clientOrderId；超时不能直接重试下单，先查询订单状态，再决定补偿。",
        "规则缓存：加载 tick size、step size、min notional、价格保护、杠杆和持仓模式，并在下单前二次校验。",
        "时间同步：签名请求依赖服务器时间；监控时钟偏差，避免因为时间戳失效导致系统误判。",
    ]:
        parts.append(bullet(text))

    parts.append(heading("五 推荐的系统技术架构", 1))
    parts.append(paragraph("一个可维护的自动交易系统可以拆成八层。策略和模型只负责产生可解释、可验证的交易意图，风险与执行层拥有最终否决权。"))
    parts.append(table(["层次", "职责", "推荐技术方案"], [
        ["数据接入", "行情快照、K 线、成交、盘口、资金费率、持仓量和交易所规则。", "交易所 WebSocket + REST；历史数据归档；统一 schema；原始消息落盘。"],
        ["标准化", "统一 symbol、时间戳、价格精度、市场类型和事件类型。", "Pydantic/JSON Schema、Decimal、UTC 时间、版本化数据契约。"],
        ["研究与回测", "特征、指标、组合、成本模型和报告。", "vectorbt、Freqtrade、Jesse、NautilusTrader 或 LEAN；结果可复现。"],
        ["策略引擎", "生成信号、候选列表和持仓目标。", "插件式策略接口；配置版本；离线与在线共用核心函数。"],
        ["风险引擎", "仓位、杠杆、止损、组合暴露、相关性、熔断和权限。", "确定性规则、硬上限、拒单原因码、人工确认和审计。"],
        ["执行引擎", "拆单、下单、撤单、订单状态机、部分成交和异常补偿。", "交易所专用 adapter；clientOrderId；REST 查询与 WebSocket 回报合并。"],
        ["持久化", "订单、成交、持仓、资金、策略版本和决策快照。", "PostgreSQL；Redis 用于短期状态和队列；原始事件可对象存储归档。"],
        ["监控与运维", "延迟、断线、拒单、滑点、PnL、风险、资金和密钥事件。", "结构化日志、Prometheus、Grafana、告警分级、值班手册和回放工具。"],
    ], [1700, 4100, 3400]))
    parts.append(heading("建议的事件流", 2))
    parts.append(paragraph("行情事件 -> 标准化 -> 特征与策略 -> 交易意图 -> 风险编译 -> 订单状态机 -> 交易所 -> 回报事件 -> 持仓与审计。"))
    parts.append(paragraph("模型服务如果存在，应被放在候选生成或解释层，而不是直接拥有下单权限。模型返回的方向、置信度和理由需要转换成结构化意图，再由本地确定性风险编译器决定是否可交易。"))

    parts.append(heading("六 回测与验证方法", 1))
    parts.append(table(["验证阶段", "必须验证的内容", "通过条件示例"], [
        ["数据质量", "时间连续性、重复、缺失、时区、合约换月、交易规则和盘口快照。", "关键字段完整；异常数据有清单并可复现。"],
        ["含成本回测", "手续费、滑点、资金费率、借贷费、点差、最小名义价值和成交延迟。", "净收益、最大回撤和换手率在不同成本假设下仍可解释。"],
        ["防止未来函数", "特征只使用当时可见的数据；信号生成、成交和结算时间严格分离。", "随机抽样审计信号；延迟一个 bar 后结果逻辑一致。"],
        ["样本外验证", "时间切分、walk-forward、跨资产、跨市场阶段和参数稳定性。", "不是只依赖单一行情阶段；参数变化不会使结果完全崩溃。"],
        ["纸面盘", "实时行情、模拟撮合、断线、部分成交、拒单和费用。", "运行一段连续周期无状态漂移，异常能恢复并留下审计。"],
        ["测试网", "真实 API 签名、权限、订单规则、保护单和部署运维。", "能完成开仓、保护、平仓、重启恢复和人工停止。"],
        ["小额实盘", "真实流动性、延迟、滑点、资金和平台行为。", "风险上限严格受控；任何异常都能快速撤单和停机。"],
    ], [1700, 4700, 2900]))
    parts.append(heading("常见回测陷阱", 2))
    for text in [
        "只用 OHLC 收盘价假设成交，忽略了盘口、点差、跳价和同一根 K 线内的止损冲突。",
        "把资金费率、手续费或借贷成本设为常数，导致长期持仓结果被高估。",
        "使用全量数据计算标准化参数、因子排名或波动率，产生未来信息泄漏。",
        "优化目标只看收益率，忽略最大回撤、尾部损失、换手、容量和策略相关性。",
        "把回测代码和实盘代码写成两套逻辑，导致成交语义、精度和持仓计算不一致。",
    ]:
        parts.append(bullet(text))

    parts.append(heading("七 风控与安全清单", 1))
    parts.append(table(["类别", "最低控制项"], [
        ["交易风险", "单笔风险、组合风险、最大杠杆、最大仓位、单日损失、连续亏损熔断、强平距离和保护单。"],
        ["流动性风险", "最大点差、最小深度、最大参与率、订单超时、部分成交和撤单失败处理。"],
        ["模型风险", "模型超时回退、本地规则兜底、置信度阈值、版本锁定、输入特征校验和漂移监控。"],
        ["操作风险", "启动门禁、测试网与生产隔离、人工急停、权限分级、变更审批和回滚。"],
        ["密钥风险", "最小权限、提现关闭、IP 白名单、定期轮换、秘密不落日志和异常登录告警。"],
        ["数据风险", "数据源故障检测、时间同步、来源标记、快照校验、历史数据版本和重放能力。"],
        ["合规风险", "按所在地和交易所规则核对身份、衍生品、杠杆、税务、记录保存和自动化交易限制。"],
    ], [2100, 7200]))
    parts.append(paragraph("建议把“是否允许下单”设计成一个硬门禁，而不是一个提示。任何关键状态未知，例如订单状态不确定、行情过期、风险参数缺失、保护单未确认或连接异常，都应默认拒绝新增风险。"))

    parts.append(heading("八 结合当前项目的落地建议", 1))
    parts.append(paragraph("当前仓库已经具备一个较完整的 Binance 优先骨架：后端包含交易所适配、WebSocket 市场流、回测、风险引擎、因子研究、执行管理、通知和前端风险配置页面。建议沿着现有边界继续演进，而不是先把所有交易所一次性抽象成相同能力。"))
    parts.append(table(["优先级", "工作项", "落地建议"], [
        ["P0", "执行与风险稳定性", "继续强化订单状态机、超时查询、保护单确认、断线重建、幂等键和全链路审计；生产环境只保留最小杠杆与仓位上限。"],
        ["P0", "回测可信度", "把手续费、滑点、资金费率、成交延迟、精度和拒单规则纳入回测；增加 walk-forward 与样本外报告。"],
        ["P1", "策略模块化", "把趋势、均值回归、因子排序、盘口信号和资金费率策略统一为策略接口，输出结构化意图，不直接下单。"],
        ["P1", "数据质量", "为 K 线、盘口、资金费率和历史归档增加来源、时间、水位、缺口和版本字段，支持重放。"],
        ["P1", "多交易所", "先建立 Binance adapter 的接口契约和 conformance tests，再逐个增加 OKX、Bybit 或 Coinbase 专用适配器；CCXT 可用于原型或补充非关键数据。"],
        ["P2", "做市与 HFT", "当前 HFT 盘口信号应继续保持 shadow 或纸面状态，先验证订单簿重建、延迟、撤单和库存模型，再考虑真实下单。"],
        ["P2", "模型接入", "模型只生成候选或解释，所有止损、仓位、杠杆、组合暴露和交易权限继续由本地确定性风控编译。"],
    ], [1100, 2500, 5700]))
    parts.append(heading("建议的三个月路线", 2))
    for text in [
        "第1阶段：整理数据契约和订单状态机，补齐异常恢复、回放、测试网验收和风险审计。",
        "第2阶段：完成两类低频策略的含成本回测、walk-forward、纸面盘和可解释报告。",
        "第3阶段：再加入一个多交易所适配器，比较真实延迟、规则差异、费用和资金占用后决定是否扩展。",
    ]:
        parts.append(bullet(text))

    parts.append(page_break())
    parts.append(heading("九 官方资料与延伸阅读", 1))
    parts.append(paragraph("以下链接均为研究时使用的公开官方文档或项目仓库。访问日期：2026年9月8日。项目版本、API 字段、费率、限频和地区可用性会变化，落地前应再次核对。"))
    sources = [
        ("S1", "Freqtrade 官方文档", "https://www.freqtrade.io/en/stable/"),
        ("S2", "Freqtrade 回测与 Hyperopt", "https://www.freqtrade.io/en/stable/backtesting/"),
        ("S3", "Hummingbot 官方网站与文档", "https://hummingbot.org/"),
        ("S4", "Hummingbot Strategies", "https://hummingbot.org/strategies/"),
        ("S5", "CCXT GitHub 与 Manual", "https://github.com/ccxt/ccxt/wiki/Manual"),
        ("S6", "Jesse 官方仓库", "https://github.com/jesse-ai/jesse"),
        ("S7", "NautilusTrader 官方文档", "https://nautilustrader.io/docs/latest/"),
        ("S8", "QuantConnect LEAN 官方仓库与文档", "https://github.com/QuantConnect/Lean"),
        ("S9", "vectorbt 官方文档", "https://vectorbt.dev/"),
        ("S10", "backtrader 官方网站", "https://www.backtrader.com/"),
        ("S11", "Binance Spot API 文档", "https://developers.binance.com/docs/binance-spot-api-docs/"),
        ("S12", "Binance USDⓈ-M Futures API 文档", "https://developers.binance.com/docs/derivatives/usds-margined-futures/"),
        ("S13", "Coinbase Advanced Trade API 概览", "https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/welcome"),
        ("S14", "Coinbase Advanced Trade WebSocket", "https://docs.cdp.coinbase.com/advanced-trade/docs/ws-overview"),
        ("S15", "OKX API v5 文档", "https://www.okx.com/docs-v5/en/"),
        ("S16", "Bybit V5 API 文档", "https://bybit-exchange.github.io/docs/v5/intro"),
        ("S17", "Kraken API 文档", "https://docs.kraken.com/api/"),
        ("S18", "Binance 公共历史数据归档", "https://data.binance.vision/"),
        ("S19", "Prometheus 官方文档", "https://prometheus.io/docs/introduction/overview/"),
        ("S20", "Grafana 官方文档", "https://grafana.com/docs/grafana/latest/"),
    ]
    for sid, title, url in sources:
        parts.append(rich_paragraph([(f"[{sid}] {title}  ", {"bold": True, "size": 18}), (url, {"size": 17, "color": "404040"})], style="Source", after=55, line=220))
    parts.append(heading("附录 研究使用说明", 1))
    parts.append(paragraph("本报告的策略章节用于技术方案比较，不代表推荐任何特定资产、交易所或策略。自动交易系统的主要失败来源往往不是指标本身，而是数据偏差、成本估计过低、订单状态不一致、风险边界缺失、密钥管理不当和异常场景没有演练。"))
    return "".join(parts)


def build() -> None:
    content_types = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/></Types>'''
    rels = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'''
    doc_rels = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/></Relationships>'''
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(OUT, "w", ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        zf.writestr("word/document.xml", document_xml(make_body()))
        zf.writestr("word/styles.xml", styles_xml())
        zf.writestr("word/footer1.xml", footer_xml())
    print(OUT.resolve())


if __name__ == "__main__":
    build()
