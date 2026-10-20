"""Validate local translation; never pass reasoning or truncated responses to inference."""
import json
import re
import requests

class PromptTranslationError(ValueError):
    pass

def validate_translation(response):
    if not isinstance(response,dict) or not isinstance(response.get('message'),dict) or not isinstance(response['message'].get('content'),str):
        raise PromptTranslationError('Invalid translation response schema.')
    if not response.get('done') or response.get('done_reason') not in (None,'stop'):
        raise PromptTranslationError('Translation incomplete or stopped by token limit.')
    content=response.get('message',{}).get('content','').strip()
    if '<think' in content.lower() or '</think>' in content.lower():
        raise PromptTranslationError('Reasoning text is not an image prompt.')
    try: value=json.loads(content)
    except (ValueError,TypeError) as exc:raise PromptTranslationError('Expected a complete JSON translation object.') from exc
    if not isinstance(value,dict) or set(value)!={'prompt'} or not isinstance(value['prompt'],str):raise PromptTranslationError('Unexpected translation schema.')
    prompt=value['prompt'].strip()
    if not 8<=len(prompt)<=1800:raise PromptTranslationError('Empty, too short or excessive translation.')
    forbidden=r"let(?:'s| us)|the user|translation request|translate(?:s|d)? to|first,? i|i need to|let me|here is|here's|analysis:|reasoning:|as an ai"
    if re.search(forbidden,prompt,re.I):raise PromptTranslationError('Explanation rejected.')
    if re.search('[çğıöşüÇĞİÖŞÜ]',prompt):raise PromptTranslationError('Source-language fragments remain.')
    if prompt.endswith((',',':',';','-','...')) or prompt.count('(')!=prompt.count(')'):
        raise PromptTranslationError('Possibly truncated translation.')
    return prompt

def translate_prompt(text, attempts=2, post=None, log=None):
    post=post or requests.post
    errors=[]
    schema={'type':'object','properties':{'prompt':{'type':'string'}},'required':['prompt'],'additionalProperties':False}
    for attempt in range(attempts):
        data=None
        try:
            response=post('http://127.0.0.1:11434/api/chat',json={'model':'qwen3:4b','think':False,'format':schema,
              'stream':False,'keep_alive':0,'options':{'num_gpu':0,'temperature':0,'num_predict':768,'num_ctx':2048},
              'messages':[{'role':'system','content':'Translate the Turkish object description to concise English using English words only. Return JSON with ONLY prompt. No reasoning, explanations, or commentary. Preserve object, materials and dimensions. Do not echo any Turkish words.'},
                          {'role':'user','content':text}]},timeout=240)
            response.raise_for_status();data=response.json();result=validate_translation(data)
            if log is not None:log.append({'attempt':attempt+1,'result':'accepted','response':data})
            return result
        except (PromptTranslationError,requests.RequestException,ValueError) as exc:
            errors.append(str(exc))
            if log is not None:log.append({'attempt':attempt+1,'result':'rejected','error':str(exc),'response':data})
    raise PromptTranslationError('Translation rejected after limited retries: '+'; '.join(errors))
