"""P8 policy round 1: carrier selection/consultation is not order progress lookup."""
import re

from agent_ops.p8_pilot import CLASSIFIER_PROMPT as BASE_CLASSIFIER
from agent_ops.p8_pilot import SILVER_PROMPT as BASE_SILVER
from agent_ops.support import Route, route_message

POLICY_REVISION='carrier-boundary-r1'
HYBRID_THRESHOLD=.9
CLARIFY_REPORT_TAXONOMY={
 'insufficient_or_ambiguous':{'name':'信息不足','expected_behavior':'应追问'},
 'out_of_scope':{'name':'超范围咨询','expected_behavior':'应转售前或告知不处理'},
 'no_action':{'name':'无需处理','expected_behavior':'致谢/确认/下单或购买计划告知，无需系统动作'},
}
BOUNDARY='''补充边界：咨询快递/物流选项、指定或更换承运商，不等于查询已经发生的订单履约。
只问承运方式、用哪家快递、能否换承运商而未要求查询已发订单进度时，归clarify。
追踪已寄包裹、问目前到哪/何时送达仍是order_status；缺订单号不改变明确的进度查询。
明确投诉、隐私或盗刷的human优先级保持，明确申请退货退款也保持原分类。
不要把“发/快递/物流”关键词本身当作已有订单查询，不根据样本编号或专名分类。'''
CLASSIFIER_PROMPT=BASE_CLASSIFIER+'\n'+BOUNDARY
SILVER_PROMPT=BASE_SILVER+'\n'+BOUNDARY
CARRIER_OPTIONS=re.compile(
 r'(?:什么|哪[家个种]?|哪个|哪种|啥).{0,3}(?:快递|物流|承运商)'
 r'|(?:指定|更换|换成|改成|改用|选择|换家|换一家|换个).{0,8}(?:快递|物流|承运商)'
 r'|(?:发|用|走|寄)(?:啥|什么|哪家|哪种)'
 r'|(?:能|可以|能否|是否支持).{0,4}(?:发|用|走|寄).{1,8}(?:吗|么)')
PROGRESS=re.compile(r'查询|查下|查一下|查查|追踪|到哪|何时送达|什么时候到|已寄|已经发|发货了没|配送进度')


def carrier_options_request(message: str) -> bool:
    return bool(CARRIER_OPTIONS.search(message)) and not bool(PROGRESS.search(message))


def evaluation_rule(message: str) -> Route:
    base=route_message(message)
    if base in {'order_status','clarify'} and carrier_options_request(message):
        return 'clarify'
    return base
