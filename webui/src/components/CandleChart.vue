<script setup lang="ts">
import { CandlestickSeries, ColorType, CrosshairMode, HistogramSeries, createChart, createSeriesMarkers, type IChartApi, type ISeriesApi, type ISeriesMarkersPluginApi, type Time, type UTCTimestamp } from 'lightweight-charts'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Candles } from '../types'

export interface Bar { t: number; o: number; h: number; l: number; c: number; v: number; prev: number | null }
const props = defineProps<{ data: Candles | null }>()
const emit = defineEmits<{ hover: [Bar | null] }>()
const el = ref<HTMLElement | null>(null)
let chart: IChartApi | null = null
let candles: ISeriesApi<'Candlestick'> | null = null
let volume: ISeriesApi<'Histogram'> | null = null
let markers: ISeriesMarkersPluginApi<Time> | null = null
let byTime = new Map<number, Bar>()
const UP = '#26c281', DOWN = '#ef5350'

onMounted(() => {
  chart = createChart(el.value!, {
    autoSize: true,
    layout: { background: { type: ColorType.Solid, color: '#0a0f15' }, textColor: '#9aa8bd', panes: { separatorColor: '#212d3b' } },
    grid: { vertLines: { color: '#131c27' }, horzLines: { color: '#131c27' } },
    crosshair: { mode: CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#212d3b' },
    timeScale: { borderColor: '#212d3b', timeVisible: true, secondsVisible: false },
    localization: { locale: 'en-GB' },
  })
  candles = chart.addSeries(CandlestickSeries, { upColor: UP, downColor: DOWN, wickUpColor: UP, wickDownColor: DOWN, borderVisible: false, priceFormat: { type: 'price', precision: 2, minMove: 0.05 } }, 0)
  volume = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false }, 1)
  chart.panes()[1]?.setHeight(110)
  markers = createSeriesMarkers(candles, [])
  chart.subscribeCrosshairMove((p) => {
    if (!p.time) { emit('hover', null); return }
    emit('hover', byTime.get(p.time as number) ?? null)
  })
  draw()
})
onBeforeUnmount(() => { chart?.remove(); chart = null })

function draw() {
  if (!chart || !candles || !volume) return
  const d = props.data
  byTime = new Map()
  if (!d || !d.n) { candles.setData([]); volume.setData([]); markers?.setMarkers([]); return }
  const daily = d.timeframe === 'daily'
  chart.applyOptions({ timeScale: { timeVisible: !daily, secondsVisible: false } })
  const cs: any[] = [], vs: any[] = []
  for (let i = 0; i < d.n; i++) {
    const t = d.t[i] as UTCTimestamp
    cs.push({ time: t, open: d.o[i], high: d.h[i], low: d.l[i], close: d.c[i] })
    vs.push({ time: t, value: d.v[i], color: d.c[i] >= d.o[i] ? 'rgba(38,194,129,.55)' : 'rgba(239,83,80,.55)' })
    byTime.set(d.t[i], { t: d.t[i], o: d.o[i], h: d.h[i], l: d.l[i], c: d.c[i], v: d.v[i], prev: i ? d.c[i - 1] : null })
  }
  candles.setData(cs)
  volume.setData(vs)
  markers?.setMarkers(d.flags.filter((f) => byTime.has(f.t)).sort((a, b) => a.t - b.t).slice(0, 400).map((f) => ({
    time: f.t as UTCTimestamp, position: 'aboveBar' as const, shape: 'circle' as const, color: '#f2a93b', text: '' })))
  chart.timeScale().fitContent()
}
watch(() => props.data, draw)
defineExpose({ reset: () => chart?.timeScale().fitContent() })
</script>
<template><div ref="el" style="position:absolute;inset:0"></div></template>
