// PROTOTYPE (#284): run every guided scenario headlessly and print its trace. Usage: node trace_scenarios.js workflow-demo.html
const fs=require('fs');const html=fs.readFileSync(process.argv[2],'utf8');
const scripts=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
const code=scripts[0]+scripts[1]+"\n"+scripts[2].split("let wid =")[0]+"\nmodule.exports={Sim,SCENARIOS,CONFIG};";
const m={exports:{}};new Function('module',code)(m);const {Sim,SCENARIOS}=m.exports;
for (const [wid,list] of Object.entries(SCENARIOS)) for (const sc of list){
  let s=Sim.init(wid);
  for (const a of sc.steps){ const ok=[...Sim.actions(s).map(x=>x.id),...(s.ended?[]:['drift','withdraw','cancel','crash'])].includes(a); if(!ok){console.log(`!! ${wid}/${sc.name}: action ${a} not available at ${s.at}`);break;} s=Sim.act(s,a);}
  console.log(`${wid} / ${sc.name}: ${s.ended?s.ended.outcome:'NOT ENDED at '+s.at} | execs: ${s.execs.map(e=>e.step+'#'+e.n+'='+e.result).join(', ')} | writes: ${s.effects.filter(e=>!e.key.includes('~finalise')).map(e=>e.kind+':'+e.status).join(', ')||'-'}`);
}
