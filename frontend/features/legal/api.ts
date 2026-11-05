import {apiFetch} from '../auth/api';

export type LegalSection = {heading:string;paragraphs:string[]};
export type LegalInfo = {
  version:string;updated_at:string;ready:boolean;operator_name:string|null;contact_email:string|null;
  terms:{title:string;sections:LegalSection[]};privacy:{title:string;sections:LegalSection[]};
  sources:{label:string;url:string}[];
};

export async function fetchLegal(): Promise<LegalInfo> {
  const r=await apiFetch('/api/v1/legal');
  if(!r.ok)throw new Error('条款暂时无法读取，请稍后重试。');
  const data:unknown=await r.json();
  const value=data as Partial<LegalInfo>|null;
  if(!value||typeof value.version!=='string'||typeof value.ready!=='boolean'||typeof value.updated_at!=='string'
    ||![value.terms,value.privacy].every(doc=>doc&&typeof doc.title==='string'&&Array.isArray(doc.sections)
      &&doc.sections.every(section=>typeof section.heading==='string'&&Array.isArray(section.paragraphs)&&section.paragraphs.every(p=>typeof p==='string')))
    ||![value.operator_name,value.contact_email].every(v=>v===null||typeof v==='string')||!Array.isArray(value.sources)
    ||!value.sources.every(s=>typeof s.label==='string'&&typeof s.url==='string'&&s.url.startsWith('https://')))
    throw new Error('条款内容返回异常，请稍后重试。');
  return value as LegalInfo;
}
