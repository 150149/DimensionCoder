import {useCallback, useEffect, useState} from 'react'
import {api} from '../api/client'
import type {ConfigSave, ConfigView, DailyTokenUsage, McpServerConfig} from '../api/types'
import ErrorToast from '../components/ErrorToast'
import {DailyTokenChart} from '../components/DailyTokenChart'

const TOKEN_RANGES = [7, 30, 90]

const emptyMcp = (): McpServerConfig => ({
    name: '', type: 'stdio', command: '', args: [], env: {}, url: '', enabled: true,
})

export default function SettingsPage() {
    const [loaded, setLoaded] = useState(false)
    const [lightBaseUrl, setLightBaseUrl] = useState('')
    const [lightModel, setLightModel] = useState('')
    const [lightApiKey, setLightApiKey] = useState('')
    const [powerBaseUrl, setPowerBaseUrl] = useState('')
    const [powerModel, setPowerModel] = useState('')
    const [powerApiKey, setPowerApiKey] = useState('')
    const [lightBaseUrl2, setLightBaseUrl2] = useState('')
    const [lightModel2, setLightModel2] = useState('')
    const [lightApiKey2, setLightApiKey2] = useState('')
    const [lightInputPrice, setLightInputPrice] = useState('')
    const [lightCachedPrice, setLightCachedPrice] = useState('')
    const [lightOutputPrice, setLightOutputPrice] = useState('')
    const [powerInputPrice, setPowerInputPrice] = useState('')
    const [powerCachedPrice, setPowerCachedPrice] = useState('')
    const [powerOutputPrice, setPowerOutputPrice] = useState('')
    const [hasLightApiKey, setHasLightApiKey] = useState(false)
    const [hasPowerApiKey, setHasPowerApiKey] = useState(false)
    const [hasLightApiKey2, setHasLightApiKey2] = useState(false)
    const [lightRouteEnabled, setLightRouteEnabled] = useState(false)
    const [projectRoot, setProjectRoot] = useState('')
    const [port, setPort] = useState('')
    const [host, setHost] = useState('')
    const [contextWindow, setContextWindow] = useState('')
    const [initialProjectRoot, setInitialProjectRoot] = useState('')
    const [channelInput, setChannelInput] = useState('')
    const [channelType, setChannelType] = useState('')
    const [hasChannel, setHasChannel] = useState(false)
    const [mcpServers, setMcpServers] = useState<McpServerConfig[]>([])
    const [mcpEditing, setMcpEditing] = useState<McpServerConfig | null>(null)
    const [mcpEditingIdx, setMcpEditingIdx] = useState(-1)
    const [mcpTestResult, setMcpTestResult] = useState<{ ok: boolean; text: string } | null>(null)
    const [toast, setToast] = useState<{ message: string; variant: 'error' | 'success' } | null>(null)
    const [tokens, setTokens] = useState<DailyTokenUsage | null>(null)
    const [tokenDays, setTokenDays] = useState(30)
    const [tokenError, setTokenError] = useState('')
    const [busy, setBusy] = useState(false)

    useEffect(() => {
        let alive = true
        api
            .getConfig()
            .then((cfg: ConfigView) => {
                if (!alive) return
                setLightBaseUrl(cfg.lightBaseUrl ?? cfg.baseUrl ?? '')
                setLightModel(cfg.lightModel)
                setPowerBaseUrl(cfg.powerBaseUrl ?? cfg.baseUrl ?? '')
                setPowerModel(cfg.powerModel)
                setHasLightApiKey(cfg.hasLightApiKey ?? cfg.hasApiKey)
                setHasPowerApiKey(cfg.hasPowerApiKey ?? cfg.hasApiKey)
                setLightBaseUrl2(cfg.lightBaseUrl2 ?? '')
                setLightModel2(cfg.lightModel2 ?? '')
                setHasLightApiKey2(cfg.hasLightApiKey2 ?? false)
                setLightRouteEnabled(cfg.lightRouteEnabled ?? false)
                setLightInputPrice(String(cfg.lightInputPrice ?? 0))
                setLightCachedPrice(String(cfg.lightCachedPrice ?? 0))
                setLightOutputPrice(String(cfg.lightOutputPrice ?? 0))
                setPowerInputPrice(String(cfg.powerInputPrice ?? 0))
                setPowerCachedPrice(String(cfg.powerCachedPrice ?? 0))
                setPowerOutputPrice(String(cfg.powerOutputPrice ?? 0))
                setProjectRoot(cfg.projectRoot)
                setPort(String(cfg.port))
                setHost(cfg.host)
                setContextWindow(String(cfg.contextWindow ?? 400000))
                setChannelType(cfg.channelType ?? '')
                setHasChannel(cfg.hasChannel ?? false)
                setMcpServers(cfg.mcpServers ?? [])
                setInitialProjectRoot(cfg.projectRoot)
                setLoaded(true)
            })
            .catch((err) => {
                if (!alive) return
                setToast({message: err instanceof Error ? err.message : '配置加载失败', variant: 'error'})
            })
        return () => {
            alive = false
        }
    }, [])

    useEffect(() => {
        let alive = true
        setTokens(null)
        setTokenError('')
        api
            .getDailyTokens(tokenDays)
            .then((d) => {
                if (!alive) return
                setTokens(d)
            })
            .catch((err: unknown) => {
                if (!alive) return
                setTokenError(err instanceof Error ? err.message : '统计加载失败')
            })
        return () => {
            alive = false
        }
    }, [tokenDays])

    const handleChannelInput = useCallback((raw: string) => {
        setChannelInput(raw)
        const trimmed = raw.trim()
        if (!trimmed) return
        try {
            const obj = JSON.parse(trimmed)
            if (obj && typeof obj === 'object' && typeof obj._type === 'string' && obj._type.toLowerCase().includes('conn')) {
                if (typeof obj.url === 'string' && obj.url) {
                    setLightBaseUrl(obj.url)
                    setPowerBaseUrl(obj.url)
                }
                if (typeof obj.key === 'string' && obj.key) {
                    setLightApiKey(obj.key)
                    setPowerApiKey(obj.key)
                }
                setChannelInput('')
                setToast({message: `已导入通道（${obj._type}）：URL / Key 已自动填充两组，请确认模型名后保存`, variant: 'success'})
            }
        } catch {
        }
    }, [])

    const handleSave = useCallback(async () => {
        if (!lightModel.trim() || !powerModel.trim()) {
            setToast({message: '请填写 light 与 power 两个模型', variant: 'error'})
            return
        }
        if ((lightBaseUrl2.trim() ? '' : 'u') !== (lightModel2.trim() ? '' : 'u')) {
            setToast({message: '备用模型：Base URL 与 Model 需同时填写（或全部留空）', variant: 'error'})
            return
        }
        const ctx = Number(contextWindow)
        if (!Number.isInteger(ctx) || ctx < 1000) {
            setToast({message: '上下文窗口必须是 ≥1000 的整数', variant: 'error'})
            return
        }
        const prices: Record<string, string> = {
            lightInputPrice, lightCachedPrice, lightOutputPrice,
            powerInputPrice, powerCachedPrice, powerOutputPrice,
        }
        for (const [k, v] of Object.entries(prices)) {
            const n = Number(v === '' ? 0 : v)
            if (!Number.isFinite(n) || n < 0) {
                setToast({message: `价格必须是 ≥0 的数字（${k}）`, variant: 'error'})
                return
            }
        }
        setBusy(true)
        try {
            const payload: ConfigSave = {
                lightModel, powerModel, projectRoot, apiKey: '',
                lightBaseUrl, lightApiKey, powerBaseUrl, powerApiKey,
                lightModel2, lightBaseUrl2, lightApiKey2,
                lightInputPrice: Number(lightInputPrice === '' ? 0 : lightInputPrice),
                lightCachedPrice: Number(lightCachedPrice === '' ? 0 : lightCachedPrice),
                lightOutputPrice: Number(lightOutputPrice === '' ? 0 : lightOutputPrice),
                powerInputPrice: Number(powerInputPrice === '' ? 0 : powerInputPrice),
                powerCachedPrice: Number(powerCachedPrice === '' ? 0 : powerCachedPrice),
                powerOutputPrice: Number(powerOutputPrice === '' ? 0 : powerOutputPrice),
                contextWindow: ctx,
                mcpServers,
            }
            await api.saveConfig(payload)
            if (projectRoot !== initialProjectRoot) {
                setToast({message: '需重启服务生效（运行 start.bat 前请确认）', variant: 'success'})
            } else {
                setToast({message: '已保存', variant: 'success'})
            }
        } catch (err) {
            setToast({message: err instanceof Error ? err.message : '保存失败', variant: 'error'})
        } finally {
            setBusy(false)
        }
    }, [lightModel, powerModel, projectRoot, contextWindow, initialProjectRoot,
        lightBaseUrl, lightApiKey, powerBaseUrl, powerApiKey,
        lightModel2, lightBaseUrl2, lightApiKey2,
        lightInputPrice, lightCachedPrice, lightOutputPrice,
        powerInputPrice, powerCachedPrice, powerOutputPrice, mcpServers])

    const handleTest = useCallback(async () => {
        setBusy(true)
        try {
            const r = await api.testLlm()
            if (r.ok) {
                setToast({message: `连接成功（${r.model ?? '模型'}）`, variant: 'success'})
            } else {
                setToast({message: `连接失败：${r.error ?? '未知错误'}`, variant: 'error'})
            }
        } catch (err) {
            setToast({message: err instanceof Error ? err.message : '测试连接失败', variant: 'error'})
        } finally {
            setBusy(false)
        }
    }, [])


    const mcpFormError = (item: McpServerConfig): string => {
        if (!item.name.trim()) return 'MCP Server 名称不能为空'
        if (item.type === 'stdio' && !item.command.trim()) return 'stdio 类型必须填 command'
        if (item.type === 'http' && !item.url.trim()) return 'http 类型必须填 url'
        const dup = mcpServers.some((s, i) => s.name.trim() === item.name.trim() && i !== mcpEditingIdx)
        if (dup) return `名称 ${item.name.trim()} 已存在（重名会覆盖配置，请换一个）`
        return ''
    }

    const saveMcpForm = useCallback(() => {
        if (!mcpEditing) return
        const err = mcpFormError(mcpEditing)
        if (err) {
            setToast({message: err, variant: 'error'});
            return
        }
        const item = {...mcpEditing, name: mcpEditing.name.trim()}
        setMcpServers((list) => {
            const next = [...list]
            if (mcpEditingIdx >= 0) next[mcpEditingIdx] = item
            else next.push(item)
            return next
        })
        setMcpEditing(null)
        setMcpTestResult(null)
    }, [mcpEditing, mcpEditingIdx, mcpServers])

    const handleTestMcp = useCallback(async () => {
        if (!mcpEditing) return
        if (mcpFormError(mcpEditing)) {
            setToast({message: '请先填好名称与连接信息', variant: 'error'});
            return
        }
        setMcpTestResult(null)
        try {
            const r = await api.testMcp({...mcpEditing, name: mcpEditing.name.trim()})
            if (r.ok) {
                setMcpTestResult({ok: true, text: `连接成功，工具 ${(r.tools ?? []).length} 个：${(r.tools ?? []).join('、') || '（无）'}`})
            } else {
                setMcpTestResult({ok: false, text: r.error ?? '未知错误'})
            }
        } catch (err) {
            setMcpTestResult({ok: false, text: err instanceof Error ? err.message : '测试失败'})
        }
    }, [mcpEditing, mcpServers])

    const renderEndpoint = (url: string, model: string, key: string, hasKey: boolean,
                            setUrl: (v: string) => void, setModel: (v: string) => void,
                            setKey: (v: string) => void,
                            modelLabel: string, modelPh: string, urlPh: string) => (
        <>
            <div className="field-row">
                <div className="field-label">Base URL</div>
                <input className="text-input" value={url} onChange={(e) => setUrl(e.target.value)}
                       placeholder={urlPh}/>
            </div>
            <div className="field-row">
                <div className="field-label">{modelLabel}</div>
                <input className="text-input" value={model} onChange={(e) => setModel(e.target.value)}
                       placeholder={modelPh}/>
            </div>
            <div className="field-row">
                <div className="field-label">API Key {hasKey ? '（已配置）' : '（未配置）'}</div>
                <input className="text-input" type="password" value={key}
                       onChange={(e) => setKey(e.target.value)}
                       placeholder="留空则保持原 Key" autoComplete="off"/>
            </div>
        </>
    )

    const renderPrices = (inP: string, cachedP: string, outP: string,
                          setIn: (v: string) => void, setCached: (v: string) => void,
                          setOut: (v: string) => void) => (
        <div className="price-grid">
            <div className="field-row">
                <div className="field-label">未缓存输入价格</div>
                <input className="text-input price-input" type="number" min={0} step={0.1} value={inP}
                       onChange={(e) => setIn(e.target.value)} placeholder="0.5"/>
            </div>
            <div className="field-row">
                <div className="field-label">缓存输入价格</div>
                <input className="text-input price-input" type="number" min={0} step={0.1} value={cachedP}
                       onChange={(e) => setCached(e.target.value)} placeholder="0.1"/>
            </div>
            <div className="field-row">
                <div className="field-label">输出价格</div>
                <input className="text-input price-input" type="number" min={0} step={0.1} value={outP}
                       onChange={(e) => setOut(e.target.value)} placeholder="1.5"/>
            </div>
        </div>
    )

    const routeFilled = !!(lightBaseUrl2.trim() && lightModel2.trim() && lightApiKey2.trim())
    const routePartial = !!(lightBaseUrl2.trim() || lightModel2.trim() || lightApiKey2.trim())
    const routeBadge = lightRouteEnabled
        ? <span className="route-badge">路由已启用</span>
        : (routePartial
            ? <span className="route-badge off">需填齐 Base URL / Model / API Key 才生效</span>
            : null)

    return (
        <div className="content-area">
            <div className="step-header">设置</div>
            {!loaded && <div className="empty-state">加载中...</div>}
            {loaded && (
                <div className="settings-form">
                    <section className="settings-card">
                        <div className="settings-card-title">Light（轻量模型）{routeBadge}</div>
                        {renderEndpoint(lightBaseUrl, lightModel, lightApiKey, hasLightApiKey,
                            setLightBaseUrl, setLightModel, setLightApiKey,
                            'Light Model', 'gpt-4o-mini', 'https://api.openai.com/v1')}
                        {renderPrices(lightInputPrice, lightCachedPrice, lightOutputPrice,
                            setLightInputPrice, setLightCachedPrice, setLightOutputPrice)}
                        <div className="subgroup">
                            <div className="settings-card-sub">备用模型（可选，双模型路由）</div>
                            <div className="settings-hint">
                                配置后 light 请求正常恒走主模型；主模型遇限流/网关/网络错误时，
                                下一次请求切换到备用模型，再下一次自动回到主模型（UI 照常提示错误）。价格按 Light 组计。
                            </div>
                            {renderEndpoint(lightBaseUrl2, lightModel2, lightApiKey2, hasLightApiKey2,
                                setLightBaseUrl2, setLightModel2, setLightApiKey2,
                                '备用 Model', '如 glm-4.5-air', 'https://备用网关/v1')}
                        </div>
                    </section>
                    <section className="settings-card">
                        <div className="settings-card-title">Power（强力模型）</div>
                        {renderEndpoint(powerBaseUrl, powerModel, powerApiKey, hasPowerApiKey,
                            setPowerBaseUrl, setPowerModel, setPowerApiKey,
                            'Power Model', 'gpt-4o', 'https://api.openai.com/v1')}
                        {renderPrices(powerInputPrice, powerCachedPrice, powerOutputPrice,
                            setPowerInputPrice, setPowerCachedPrice, setPowerOutputPrice)}
                    </section>
                    <section className="settings-card">
                        <div className="settings-card-title">工作区与服务</div>
                        <div className="field-grid-2">
                            <div className="field-row">
                                <div className="field-label">项目目录（Project Root）</div>
                                <input className="text-input" value={projectRoot}
                                       onChange={(e) => setProjectRoot(e.target.value)} placeholder="workspace"/>
                            </div>
                            <div className="field-row">
                                <div className="field-label">上下文窗口大小（Context Window）</div>
                                <input className="text-input" type="number" min={1000} step={1000}
                                       value={contextWindow}
                                       onChange={(e) => setContextWindow(e.target.value)}
                                       placeholder="400000"
                                       title="模型上下文总容量（token），用于输入框上方的占用百分比计算"/>
                            </div>
                            <div className="field-row">
                                <div className="field-label">服务端口（只读）</div>
                                <input className="text-input" value={port} readOnly/>
                            </div>
                            <div className="field-row">
                                <div className="field-label">监听地址（只读）</div>
                                <input className="text-input" value={host} readOnly/>
                            </div>
                        </div>
                    </section>
                    <section className="settings-card wide">
                        <div className="settings-card-title">
                            每日 Token 消耗
                            <span className="dtc-ranges">
                                {TOKEN_RANGES.map((n) => (
                                    <button key={n} type="button" className="dtc-range"
                                            aria-pressed={tokenDays === n}
                                            onClick={() => setTokenDays(n)}>近 {n} 天</button>
                                ))}
                            </span>
                        </div>
                        {tokenError && (
                            <div className="settings-hint">统计加载失败：{tokenError}</div>
                        )}
                        {!tokenError && !tokens && (
                            <div className="settings-hint">统计加载中...</div>
                        )}
                        {!tokenError && tokens && <DailyTokenChart items={tokens.items}/>}
                        <div className="settings-hint">
                            统计口径：按步骤最后一条活动的日期归日（跨日长步骤的全部累计量记在最后一天、
                            步骤重跑会归到新最后一天），是活动归属而非逐日账单；全部任务与审查/收尾步骤均计入。
                        </div>
                    </section>
                    <section className="settings-card wide">
                        <div className="settings-card-title">New API 通道导入（可选）</div>
                        {hasChannel && (
                            <div className="settings-hint">当前使用通道：{channelType}（来自 config.json llmChannel，未配新字段时回退）</div>
                        )}
                        <div className="field-row">
                            <div className="field-label">通道 JSON（粘贴自动填充 Light/Power 两组）</div>
                            <textarea
                                className="text-input"
                                rows={2}
                                value={channelInput}
                                onChange={(e) => handleChannelInput(e.target.value)}
                                placeholder={'粘贴通道 JSON 自动填充两组，如 {"_type":"newapi_channel_conn","key":"sk-...","url":"https://..."}'}
                            />
                        </div>
                    </section>
                    <section className="settings-card wide">
                        <div className="settings-card-title">MCP Servers（外接工具，连接成功后所有角色可用）</div>
                        {mcpServers.length === 0 && !mcpEditing && (
                            <div className="settings-hint">尚未配置 MCP Server</div>
                        )}
                        {mcpServers.map((s, i) => (
                            <div className="field-row" key={`${s.name}-${i}`}>
                                <div className="field-label">
                                    <label>
                                        <input type="checkbox" checked={s.enabled}
                                               onChange={() => setMcpServers(mcpServers.map((x, j) => j === i ? {...x, enabled: !x.enabled} : x))}/>
                                        {' '}{s.name}
                                    </label>
                                    <span>（{s.type === 'stdio' ? `stdio：${s.command} ${s.args.join(' ')}` : `http：${s.url}`}）</span>
                                </div>
                                <div>
                                    <button className="btn" onClick={() => {
                                        setMcpEditingIdx(i);
                                        setMcpEditing({...s});
                                        setMcpTestResult(null)
                                    }} disabled={!!mcpEditing}>编辑
                                    </button>
                                    {' '}
                                    <button className="btn" onClick={() => setMcpServers(mcpServers.filter((_, j) => j !== i))} disabled={!!mcpEditing}>删除</button>
                                </div>
                            </div>
                        ))}
                        {!mcpEditing && (
                            <div>
                                <button className="btn" onClick={() => {
                                    setMcpEditingIdx(-1);
                                    setMcpEditing(emptyMcp());
                                    setMcpTestResult(null)
                                }}>
                                    + 添加 MCP Server
                                </button>
                            </div>
                        )}
                        {mcpEditing && (
                            <div className="settings-hint" style={{display: 'flex', flexDirection: 'column', gap: 6}}>
                                <div className="field-row">
                                    <div className="field-label">名称</div>
                                    <input className="text-input" value={mcpEditing.name}
                                           onChange={(e) => setMcpEditing({...mcpEditing, name: e.target.value})}
                                           placeholder="如 fs / weather"/>
                                </div>
                                <div className="field-row">
                                    <div className="field-label">类型</div>
                                    <select className="text-input" value={mcpEditing.type}
                                            onChange={(e) => setMcpEditing({...mcpEditing, type: e.target.value as 'stdio' | 'http'})}>
                                        <option value="stdio">stdio（本地命令）</option>
                                        <option value="http">http（远程地址）</option>
                                    </select>
                                </div>
                                {mcpEditing.type === 'stdio' ? (
                                    <>
                                        <div className="field-row">
                                            <div className="field-label">启动命令（command）</div>
                                            <input className="text-input" value={mcpEditing.command}
                                                   onChange={(e) => setMcpEditing({...mcpEditing, command: e.target.value})}
                                                   placeholder="如 npx / python / node"/>
                                        </div>
                                        <div className="field-row">
                                            <div className="field-label">参数（每行一条）</div>
                                            <textarea className="text-input" rows={3} value={mcpEditing.args.join('\n')}
                                                      onChange={(e) => setMcpEditing({...mcpEditing, args: e.target.value.split('\n').filter((l) => l.trim() !== '')})}
                                                      placeholder={'如：\n-y\n@modelcontextprotocol/server-filesystem\nD:\\workspace'}/>
                                        </div>
                                        <div className="field-row">
                                            <div className="field-label">环境变量（每行 KEY=VALUE）</div>
                                            <textarea className="text-input" rows={2}
                                                      value={Object.entries(mcpEditing.env).map(([k, v]) => `${k}=${v}`).join('\n')}
                                                      onChange={(e) => {
                                                          const env: Record<string, string> = {}
                                                          for (const line of e.target.value.split('\n')) {
                                                              const idx = line.indexOf('=')
                                                              if (idx > 0) env[line.slice(0, idx).trim()] = line.slice(idx + 1)
                                                          }
                                                          setMcpEditing({...mcpEditing, env})
                                                      }}
                                                      placeholder={'如：\nAPI_TOKEN=sk-...'}/>
                                        </div>
                                    </>
                                ) : (
                                    <div className="field-row">
                                        <div className="field-label">服务地址（URL）</div>
                                        <input className="text-input" value={mcpEditing.url}
                                               onChange={(e) => setMcpEditing({...mcpEditing, url: e.target.value})}
                                               placeholder="https://example.com/mcp"/>
                                    </div>
                                )}
                                {mcpTestResult && (
                                    <div style={{color: mcpTestResult.ok ? 'var(--accent, #2e7d32)' : 'var(--error, #c62828)', wordBreak: 'break-all'}}>
                                        {mcpTestResult.text}
                                    </div>
                                )}
                                <div className="settings-actions">
                                    <button className="btn btn-primary" onClick={saveMcpForm}>确定</button>
                                    <button className="btn" onClick={handleTestMcp}>测试连接</button>
                                    <button className="btn" onClick={() => {
                                        setMcpEditing(null);
                                        setMcpTestResult(null)
                                    }}>取消
                                    </button>
                                </div>
                            </div>
                        )}
                    </section>
                    <div className="settings-actions">
                        <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
                            保存
                        </button>
                        <button className="btn" onClick={handleTest} disabled={busy}>
                            测试连接
                        </button>
                    </div>
                </div>
            )}
            {toast && <ErrorToast message={toast.message} variant={toast.variant} onClose={() => setToast(null)}/>}
        </div>
    )
}
