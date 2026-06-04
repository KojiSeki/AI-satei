import { useEffect, useMemo, useRef, useState } from 'react'
import type { ChangeEvent, DragEvent } from 'react'
import './App.css'

type SateiItem = {
  id: number
  upload_record_id: number
  asset_id: string | null
  maker: string | null
  category: string | null
  original_price: number | null
  price: number | null
  raw_text: string | null
  status: string
}

type UploadResult = {
  id: number
  filename: string
  size_bytes: number
  summary?: string
  status: string
  parse_mode?: 'table' | 'free_text' | 'plain_text'
  record_count?: number
  line_count?: number
  detected_asset_count?: number
  priced_count?: number
  estimated_total?: number
  category_counts?: Record<string, number>
  maker_counts?: Record<string, number>
  items?: SateiItem[]
}

type InputMode = 'file' | 'text'

function App() {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [apiStatus, setApiStatus] = useState<'checking' | 'ok' | 'down'>('checking')
  const [mode, setMode] = useState<InputMode>('file')
  const [file, setFile] = useState<File | null>(null)
  const [text, setText] = useState('')
  const [isDragging, setIsDragging] = useState(false)
  const [isUploading, setIsUploading] = useState(false)
  const [message, setMessage] = useState('')
  const [result, setResult] = useState<UploadResult | null>(null)
  const [items, setItems] = useState<SateiItem[]>([])

  const updateResult = (payload: UploadResult | null) => {
    setResult(payload)
    setItems(payload?.items ?? [])
  }

  useEffect(() => {
    fetch('/health')
      .then((response) => {
        if (!response.ok) {
          throw new Error('health check failed')
        }
        return response.json()
      })
      .then(() => setApiStatus('ok'))
      .catch(() => setApiStatus('down'))
  }, [])

  const fileSize = useMemo(() => {
    if (!file) {
      return ''
    }
    if (file.size < 1024 * 1024) {
      return `${Math.max(1, Math.round(file.size / 1024))} KB`
    }
    return `${(file.size / 1024 / 1024).toFixed(1)} MB`
  }, [file])

  const textStats = useMemo(() => {
    const lineCount = text.length ? text.split(/\r\n|\r|\n/).length : 0
    return `${lineCount} 行 / ${text.length} 文字`
  }, [text])

  const formatBytes = (bytes: number) => {
    if (bytes < 1024) {
      return `${bytes} B`
    }
    if (bytes < 1024 * 1024) {
      return `${Math.round(bytes / 1024)} KB`
    }
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  }

  const formatYen = (value: number) => `${value.toLocaleString('ja-JP')} 円`

  const hasEntries = (value: Record<string, number> | undefined) =>
    value !== undefined && Object.keys(value).length > 0

  const pickFile = (selected: File | undefined) => {
    if (!selected) {
      return
    }

    setFile(selected)
    setMessage('')
    updateResult(null)
  }

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    pickFile(event.target.files?.[0])
  }

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    if (mode !== 'file') {
      return
    }

    event.preventDefault()
    setIsDragging(false)
    pickFile(event.dataTransfer.files?.[0])
  }

  const uploadFile = async () => {
    if (!file) {
      setMessage('ファイルを選択してください')
      return
    }

    const formData = new FormData()
    formData.append('file', file)
    setIsUploading(true)
    setMessage('')

    try {
      const response = await fetch('/upload', {
        method: 'POST',
        body: formData,
      })
      const payload = await response.json()

      if (!response.ok) {
        throw new Error(payload.detail ?? 'アップロードに失敗しました')
      }

      updateResult(payload)
      setMessage('アップロード完了')
    } catch (error) {
      updateResult(null)
      setMessage(error instanceof Error ? error.message : 'アップロードに失敗しました')
    } finally {
      setIsUploading(false)
    }
  }

  const uploadText = async () => {
    if (!text.trim()) {
      setMessage('テキストを貼り付けてください')
      return
    }

    const formData = new FormData()
    formData.append('text', text)
    formData.append('filename', 'pasted-text.txt')
    setIsUploading(true)
    setMessage('')

    try {
      const response = await fetch('/upload-text', {
        method: 'POST',
        body: formData,
      })
      const payload = await response.json()

      if (!response.ok) {
        throw new Error(payload.detail ?? 'テキスト送信に失敗しました')
      }

      updateResult(payload)
      setMessage('テキスト取込完了')
    } catch (error) {
      updateResult(null)
      setMessage(error instanceof Error ? error.message : 'テキスト送信に失敗しました')
    } finally {
      setIsUploading(false)
    }
  }

  const handleCellChange = (itemId: number, field: keyof SateiItem, value: any) => {
    setItems((prev) =>
      prev.map((item) => (item.id === itemId ? { ...item, [field]: value } : item))
    )
  }

  const handleCellBlur = async (itemId: number, field: keyof SateiItem) => {
    const item = items.find((it) => it.id === itemId)
    if (!item) return

    try {
      const response = await fetch(`/satei-items/${itemId}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ [field]: item[field] }),
      })
      if (!response.ok) {
        throw new Error('更新に失敗しました')
      }
      const updatedItem = await response.json()

      // 全体の再集計
      const updatedItems = items.map((it) => (it.id === itemId ? { ...it, ...updatedItem } : it))
      const total = updatedItems.reduce((acc, cur) => acc + (cur.price ?? 0), 0)
      const priced = updatedItems.filter((it) => it.price !== null && it.price !== undefined).length

      if (result) {
        setResult({
          ...result,
          estimated_total: total,
          priced_count: priced,
          items: updatedItems,
        })
      }
    } catch (error) {
      console.error(error)
      if (result?.items) {
        setItems(result.items)
      }
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">AI Satei</p>
          <h1>査定データ取込</h1>
        </div>
        <span className={`status-pill ${apiStatus}`}>
          {apiStatus === 'checking' && 'API 確認中'}
          {apiStatus === 'ok' && 'API 正常'}
          {apiStatus === 'down' && 'API 停止'}
        </span>
      </header>

      <section className="workspace">
        <div
          className={`dropzone ${isDragging ? 'dragging' : ''}`}
          onDragOver={(event) => {
            if (mode !== 'file') {
              return
            }
            event.preventDefault()
            setIsDragging(true)
          }}
          onDragLeave={() => setIsDragging(false)}
          onDrop={handleDrop}
        >
          <div className="mode-toggle" role="tablist" aria-label="取込方法">
            <button
              type="button"
              className={mode === 'file' ? 'active' : ''}
              onClick={() => setMode('file')}
            >
              ファイル
            </button>
            <button
              type="button"
              className={mode === 'text' ? 'active' : ''}
              onClick={() => setMode('text')}
            >
              テキスト
            </button>
          </div>

          {mode === 'file' ? (
            <>
              <div className="document-stack" aria-hidden="true">
                <span />
                <span />
                <span />
              </div>
              <div className="dropzone-copy">
                <h2>{file ? file.name : 'CSV / Excel / PDF'}</h2>
                <p>{file ? fileSize : 'ドラッグ＆ドロップまたはファイル選択'}</p>
              </div>
              <input
                ref={inputRef}
                type="file"
                accept=".csv,.xls,.xlsx,.pdf"
                onChange={handleFileChange}
              />
              <div className="actions">
                <button type="button" className="secondary" onClick={() => inputRef.current?.click()}>
                  ファイル選択
                </button>
                <button
                  type="button"
                  onClick={uploadFile}
                  disabled={isUploading || apiStatus !== 'ok'}
                >
                  {isUploading ? '送信中' : 'アップロード'}
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="paste-panel">
                <textarea
                  value={text}
                  onChange={(event) => {
                    setText(event.target.value)
                    setMessage('')
                    updateResult(null)
                  }}
                  placeholder="ここにテキストを貼り付け"
                />
                <p>{textStats}</p>
              </div>
              <div className="actions">
                <button type="button" className="secondary" onClick={() => setText('')}>
                  クリア
                </button>
                <button
                  type="button"
                  onClick={uploadText}
                  disabled={isUploading || apiStatus !== 'ok' || !text.trim()}
                >
                  {isUploading ? '送信中' : 'テキスト取込'}
                </button>
              </div>
            </>
          )}
        </div>

        <aside className="result-panel">
          <h2>取込結果</h2>
          {message && <p className="message">{message}</p>}
          {result ? (
            <>
              <dl>
                <div>
                  <dt>ID</dt>
                  <dd>{result.id}</dd>
                </div>
                <div>
                  <dt>ファイル名</dt>
                  <dd>{result.filename}</dd>
                </div>
                <div>
                  <dt>サイズ</dt>
                  <dd>{formatBytes(result.size_bytes)}</dd>
                </div>
                {typeof result.record_count === 'number' && (
                  <div>
                    <dt>明細件数</dt>
                    <dd>{result.record_count.toLocaleString('ja-JP')} 件</dd>
                  </div>
                )}
                {typeof result.line_count === 'number' && !result.record_count && (
                  <div>
                    <dt>行数</dt>
                    <dd>{result.line_count.toLocaleString('ja-JP')} 行</dd>
                  </div>
                )}
                {typeof result.detected_asset_count === 'number' &&
                  result.detected_asset_count > 0 && (
                    <div>
                      <dt>管理番号候補</dt>
                      <dd>{result.detected_asset_count.toLocaleString('ja-JP')} 件</dd>
                    </div>
                  )}
                {typeof result.estimated_total === 'number' &&
                  typeof result.priced_count === 'number' &&
                  result.priced_count > 0 && (
                  <div>
                    <dt>推定卸価格合計</dt>
                    <dd>{formatYen(result.estimated_total)}</dd>
                  </div>
                )}
                {typeof result.priced_count === 'number' && result.priced_count > 0 && (
                  <div>
                    <dt>価格入力済</dt>
                    <dd>{result.priced_count.toLocaleString('ja-JP')} 件</dd>
                  </div>
                )}
                <div>
                  <dt>概要</dt>
                  <dd>{result.summary ?? result.status}</dd>
                </div>
              </dl>
              {hasEntries(result.category_counts) && (
                <div className="category-summary">
                  <h3>カテゴリ別</h3>
                  <ul>
                    {Object.entries(result.category_counts ?? {})
                      .sort(([, left], [, right]) => right - left)
                      .map(([category, count]) => (
                        <li key={category}>
                          <span>{category}</span>
                          <strong>{count.toLocaleString('ja-JP')}</strong>
                        </li>
                      ))}
                    </ul>
                </div>
              )}
              {hasEntries(result.maker_counts) && (
                <div className="category-summary">
                  <h3>メーカー候補</h3>
                  <ul>
                    {Object.entries(result.maker_counts ?? {})
                      .sort(([, left], [, right]) => right - left)
                      .map(([maker, count]) => (
                        <li key={maker}>
                          <span>{maker}</span>
                          <strong>{count.toLocaleString('ja-JP')}</strong>
                        </li>
                      ))}
                  </ul>
                </div>
              )}
            </>
          ) : (
            <p className="empty">アップロード後に結果が表示されます</p>
          )}
        </aside>
      </section>

      {items.length > 0 && (
        <section className="satei-items-section">
          <div className="section-header">
            <h2>査定明細一覧（一台一台の査定・編集）</h2>
            <p className="section-subheader">
              管理番号、メーカー、カテゴリ、査定額を入力または修正してください。フォーカスアウト（欄外クリック）で自動保存されます。
            </p>
          </div>
          <div className="table-responsive">
            <table className="satei-table">
              <thead>
                <tr>
                  <th>No.</th>
                  <th>管理番号</th>
                  <th>メーカー</th>
                  <th>カテゴリ</th>
                  <th>査定額 (円)</th>
                  <th>元価格 (円)</th>
                  <th>ステータス</th>
                  <th>元のテキスト (参考)</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item, idx) => (
                  <tr key={item.id}>
                    <td>{idx + 1}</td>
                    <td>
                      <input
                        type="text"
                        className="table-input"
                        value={item.asset_id ?? ''}
                        onChange={(e) => handleCellChange(item.id, 'asset_id', e.target.value)}
                        onBlur={() => handleCellBlur(item.id, 'asset_id')}
                        placeholder="未登録"
                      />
                    </td>
                    <td>
                      <input
                        type="text"
                        className="table-input"
                        value={item.maker ?? ''}
                        onChange={(e) => handleCellChange(item.id, 'maker', e.target.value)}
                        onBlur={() => handleCellBlur(item.id, 'maker')}
                        placeholder="未登録"
                      />
                    </td>
                    <td>
                      <input
                        type="text"
                        className="table-input"
                        value={item.category ?? ''}
                        onChange={(e) => handleCellChange(item.id, 'category', e.target.value)}
                        onBlur={() => handleCellBlur(item.id, 'category')}
                        placeholder="未登録"
                      />
                    </td>
                    <td>
                      <input
                        type="number"
                        className="table-input price-input"
                        value={item.price ?? ''}
                        onChange={(e) =>
                          handleCellChange(
                            item.id,
                            'price',
                            e.target.value === '' ? null : parseInt(e.target.value, 10)
                          )
                        }
                        onBlur={() => handleCellBlur(item.id, 'price')}
                        placeholder="0"
                      />
                    </td>
                    <td>
                      <span className="original-price">
                        {item.original_price !== null ? formatYen(item.original_price) : '-'}
                      </span>
                    </td>
                    <td>
                      <select
                        className="table-select"
                        value={item.status}
                        onChange={(e) => {
                          handleCellChange(item.id, 'status', e.target.value)
                          setTimeout(() => handleCellBlur(item.id, 'status'), 0)
                        }}
                      >
                        <option value="pending">未査定</option>
                        <option value="completed">査定済</option>
                      </select>
                    </td>
                    <td className="raw-text-cell" title={item.raw_text ?? ''}>
                      {item.raw_text ?? '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </main>
  )
}

export default App
