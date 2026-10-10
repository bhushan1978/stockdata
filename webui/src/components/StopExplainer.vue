<script setup lang="ts">
defineProps<{ compact?: boolean }>()
</script>
<template>
  <div class="grid g3" style="align-items:stretch">
    <div class="panel" style="margin:0"><h3>Stopping a download</h3>
      <p class="small muted" style="margin:0 0 6px">Job Queue → <b>Pause</b> or <b>Stop</b>.</p>
      <p class="small" style="margin:0">The worker finishes the window it is on, saves it, and exits. Nothing is lost or repeated; the job waits to be resumed. This is the normal way to stop for the day.</p></div>
    <div class="panel" style="margin:0"><h3>Stopping the application</h3>
      <p class="small muted" style="margin:0 0 6px">Settings → Stopping, or <code>stop_stockdata.bat</code>.</p>
      <p class="small" style="margin:0">Shuts down the web server. You choose: <b>pause downloads first</b> (recommended) or <b>leave them running</b> in the background. A running download is never killed by this.</p></div>
    <div class="panel" style="margin:0"><h3>Force-killing a process</h3>
      <p class="small muted" style="margin:0 0 6px">Emergency only: <b>Force stop</b> on a job, or <code>stop_stockdata.bat --force</code>.</p>
      <p class="small" style="margin:0">Ends the process at once. Saved windows are safe; the window in flight is requested again. Use it only for something hung. The next start marks affected jobs <i>Interrupted</i> so you can resume them.</p></div>
  </div>
</template>
