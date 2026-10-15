const stageNames: Record<string, string> = {
  normalizing_requirements: '整理审查要求', queued: '等待审查资源', parsing: '解析文件', parse_main: '解析主文件',
  proofreading: '主文件文字校对', extracting: '提取事实', facts: '提取事实', main: '主文件审查', main_review: '主文件审查',
  main_consistency: '主文件一致性审查', main_verify: '复核主文件证据', cross: '跨文件一致性审查', cross_review: '跨文件一致性审查',
  verify: '原文证据复核', verification: '原文证据复核', exporting: '生成报告与批注', export: '生成报告与批注',
  completed: '审查完成', done: '审查完成', cancelled: '已取消', failed: '审查失败', partial: '部分审查完成', starting: '准备审查',
  '主文件文字校对与事实提取': '主文件校对与事实提取', '主文件全文数值与语义一致性': '主文件一致性审查',
  '主文件证据复核': '主文件证据复核', '主文件自定义要点内容搜索': '核对主文件专项要求',
  '跨文件自定义要点内容搜索': '核对跨文件专项要求', '独立核查自定义内容缺失': '复核内容缺失',
  '审查完成，准备导出': '准备生成审查报告',
}

const stagePrefixes: [string, string][] = [
  ['主文件自定义要点：', '主文件专项审查'],
  ['跨文件自定义要点：', '跨文件专项审查'],
  ['提取其他文件事实：', '提取对照文件事实'],
  ['与主文件交叉比对：', '跨文件一致性审查'],
  ['跨文件证据复核：', '跨文件证据复核'],
]

export function reviewStageName(stage: string): string {
  if (Object.hasOwn(stageNames, stage)) return stageNames[stage]
  return stagePrefixes.find(([prefix]) => stage.startsWith(prefix))?.[1] ?? '正在审查'
}
