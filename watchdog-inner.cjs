// Runs inside WSL (started by watchdog.cjs with PACER_ENV, see pacer-config.cjs). Detects hung heartbeat runs
// (pi alive but agent_settled / silent), kills them, cancels, releases stale leases.
const fs=require("fs"),path=require("path");
const E=JSON.parse(process.env.PACER_ENV);
const {Client}=require(E.pkg+"/node_modules/pg");
const LOGS=E.instance+"/data/run-logs/";
const SETTLED_MS=5*60e3, SILENT_MS=45*60e3;
(async()=>{
  const out={hung:[],cancelFailed:false,leases:0};
  const c=new Client({host:E.db.host,port:E.db.port,user:E.db.user,password:E.db.password,database:E.db.database});await c.connect();
  const rs=(await c.query("select id,agent_id,process_pid,log_ref from heartbeat_runs where status=$1",["running"])).rows;
  for(const r of rs){
    let st;try{st=fs.statSync(LOGS+r.log_ref)}catch{continue}
    const age=Date.now()-st.mtimeMs;
    const fd=fs.openSync(LOGS+r.log_ref,"r");const n=Math.min(st.size,6000);const b=Buffer.alloc(n);fs.readSync(fd,b,0,n,st.size-n);fs.closeSync(fd);
    const lines=b.toString().trim().split("\n").filter(Boolean);
    const last=lines.length?lines[lines.length-1]:"";
    const settled=last.includes("agent_settled");
    if(!((settled&&age>SETTLED_MS)||age>SILENT_MS))continue;
    out.hung.push({run:r.id,pid:r.process_pid,ageMin:Math.round(age/60e3),settled});
    if(r.process_pid){try{process.kill(r.process_pid,"SIGKILL")}catch{}}
    try{const x=await fetch(E.url+"/api/heartbeat-runs/"+r.id+"/cancel",{method:"POST",headers:{"content-type":"application/json"},body:"{}",signal:AbortSignal.timeout(20000)});if(!x.ok)out.cancelFailed=true}catch{out.cancelFailed=true}
  }
  out.leases=(await c.query("UPDATE environment_leases SET status=$1, released_at=now(), updated_at=now() WHERE status=$2 AND heartbeat_run_id IN (select id from heartbeat_runs where status in ('cancelled','interrupted','failed','succeeded'))",["released","active"])).rowCount;
  await c.end();console.log(JSON.stringify(out));
})().catch(e=>{console.log(JSON.stringify({error:String(e)}));process.exit(1)});
