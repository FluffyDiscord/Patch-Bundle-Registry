import assert from "node:assert/strict";
import test from "node:test";
import { matchingSources } from "../../shared/source-picker.js";
import { bindActions, iconAction } from "../../shared/actions.js";
import { createBrowsePreferences } from "../../shared/preferences.js";

test("Source search matches multiple terms across repository labels and hosts", () => {
    const options = [{label:"All sources", value:""},
        {label:"Team / Video",value:"https://github.com/team/video"},
        {label:"Team / Music",value:"https://git.example/team/music"}];
    assert.equal(matchingSources(options, "").length, 3);
    assert.deepEqual(matchingSources(options, " TEAM github "), options.slice(0,2));
    assert.deepEqual(matchingSources(options, "music git.example"), [options[0],options[2]]);
    assert.deepEqual(matchingSources(options, "missing"), [options[0]]);
});

test("New tabs default to Latest while explicit selections and legacy types survive", () => {
    const prefs = createBrowsePreferences(() => ({getItem:()=>JSON.stringify({
        tabs:{bundles:{release:"release",type:"ReSeam"},apps:{search:"music"}}
    })}));
    assert.equal(prefs.read("bundles").release,"release");
    assert.equal(prefs.read("bundles").type,"Reseam");
    assert.equal(prefs.read("apps").release,"latest");
    assert.equal(prefs.read("apps").search,"music");
});

test("Touch holds show labels without performing an action, and normal taps copy", async t => {
    t.mock.timers.enable({apis:["setTimeout"]});
    const classes = new Set(), attributes = new Map(), tip = {};
    const action = Object.assign(new EventTarget(), {
        dataset:{copyText:"com.example.video",tooltip:"Copy package name"},
        classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)},
        hasAttribute:name=>name==="data-copy-text",
        setAttribute:(key,value)=>attributes.set(key,value),
        querySelector:()=>tip
    });
    const previous = Object.getOwnPropertyDescriptor(globalThis,"navigator");
    const copied = [];
    Object.defineProperty(globalThis,"navigator",{configurable:true,value:{
        clipboard:{writeText:async value=>copied.push(value)}
    }});
    t.after(()=>{
        if(previous) Object.defineProperty(globalThis,"navigator",previous);
        else delete globalThis.navigator;
    });
    bindActions({querySelectorAll:()=>[action]});
    action.dispatchEvent(Object.assign(new Event("pointerdown"),{pointerType:"touch"}));
    t.mock.timers.tick(500);
    assert(classes.has("tooltip-visible"));
    action.dispatchEvent(new Event("pointerup"));
    const heldClick = new Event("click",{cancelable:true});
    action.dispatchEvent(heldClick);
    assert(heldClick.defaultPrevented);
    assert.deepEqual(copied,[]);
    t.mock.timers.tick(1800);
    action.dispatchEvent(new Event("click",{cancelable:true}));
    await new Promise(resolve=>setImmediate(resolve));
    assert.deepEqual(copied,["com.example.video"]);
    assert.equal(tip.textContent,"Copied!");
    t.mock.timers.tick(1800);
    assert.equal(attributes.get("aria-label"),"Copy package name");
    assert(!classes.has("tooltip-visible"));
});

test("Icon links preserve downloads and external repository navigation", () => {
    const escape = value=>String(value).replaceAll('"',"&quot;");
    const link = iconAction("download","Download bundle",escape,{href:"https://example.test/bundle"});
    assert.match(link,/href="https:\/\/example.test\/bundle"/);
    assert.match(link,/aria-label="Download bundle"/);
    assert.match(link,/target="_blank" rel="noopener noreferrer"/);
    const disabled = iconAction("link","Copy URL",escape,{copy:"https://api.test",disabled:true});
    assert.match(disabled,/disabled/);
    assert.match(disabled,/data-copy-text="https:\/\/api.test"/);
});


// Exercise popup selection and refresh while keeping search outside the scroll list.
test("Source popup searches, updates its live list, selects a source and restores focus", async t => {
    const {createSourcePicker} = await import("../../shared/source-picker.js");
    function node() {
        return {handlers:{},attrs:{},value:"",isConnected:true,dataset:{},
            addEventListener(name,fn){this.handlers[name]=fn;},
            setAttribute(name,value){this.attrs[name]=value;},
            focus(){this.focused=true;}, classList:{add(){},remove(){}}};
    }
    const button = node(), label = node(), search = node(), body = node(), count = node(), close = node();
    button.querySelector=()=>label;
    const dialog = node();
    dialog.querySelector = selector=>({".details-search":search,".source-options":body,
        ".details-count":count,".dialog-close":close})[selector];
    dialog.showModal=()=>{dialog.open=true;};
    dialog.close=()=>{dialog.open=false;dialog.handlers.close();};
    let choices;
    body.querySelectorAll=()=> {
        choices=[...body.innerHTML.matchAll(/data-source="([^"]*)"/g)].map(match=>{
            const choice=node();choice.dataset.source=match[1];return choice;
        });
        return choices;
    };
    const prior = globalThis.document;
    globalThis.document={activeElement:button,createElement:()=>dialog,
        body:{appendChild(){}},documentElement:node()};
    t.after(()=>{globalThis.document=prior;});
    const select={value:"",options:[{label:"All sources",value:""},
        {label:"Video patches",value:"https://github.com/o/video"},
        {label:"Music patches",value:"https://git.example/o/music"}]};
    const selected=[];
    const picker=createSourcePicker({button,select,onSelect:value=>selected.push(value),
        escapeHtml:value=>String(value)});
    button.handlers.click();
    assert(dialog.open);
    assert(search.focused);
    assert.equal(button.attrs["aria-expanded"],"true");
    search.value="music";search.handlers.input();
    assert.equal(count.textContent,"1 source");
    assert.doesNotMatch(body.innerHTML,/Video patches/);
    select.options.push({label:"Music extras",value:"https://github.com/o/music-extra"});
    picker.sync();
    assert.equal(count.textContent,"2 sources");
    assert.equal(search.value,"music");
    choices.find(choice=>choice.dataset.source==="https://git.example/o/music").handlers.click();
    assert.deepEqual(selected,["https://git.example/o/music"]);
    assert.equal(label.textContent,"Music patches");
    assert(!dialog.open);
    assert(button.focused);
    assert.equal(button.attrs["aria-expanded"],"false");
});

