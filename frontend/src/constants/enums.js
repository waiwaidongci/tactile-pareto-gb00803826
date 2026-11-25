export const roles = {analyst: '分析员', reviewer: '复核员', viewer: '只读访客'};
export const states = {queued: '排队', running: '计算中', completed: '已计算', failed: '需修正'};
export const decisions = {approved: '已批准', returned: '已退回'};
export const actions = {import:'导入数据',queue:'提交计算',completed:'完成计算',failed:'计算失败',approved:'批准发布',returned:'退回结果',schedule:'建立计划',cancel:'停用计划',delivered:'报告入箱',shift_create:'建立班次',shift_upload:'交单/补交原表',shift_pass:'逐件核对通过',shift_mismatch:'核对发现不符',shift_close:'封班'};
export const shiftStatuses = {collecting: '收单中', mismatch: '核对不符', ready: '核对通过', closed: '已封班'};
