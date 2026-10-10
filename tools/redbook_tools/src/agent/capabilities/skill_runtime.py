"""Read only immutable, explicitly selected skill resources; never run scripts."""
from pathlib import PurePosixPath

from jsonschema import Draft202012Validator

from .models import CapabilityError


RESOURCE_SCHEMA={'type':'object','properties':{
    'path':{'type':'string','minLength':1,'maxLength':256},
    'offset':{'type':'integer','minimum':0},
    'limit':{'type':'integer','minimum':1,'maximum':12000}},'required':['path'],'additionalProperties':False}


def resource_tools(skills):
    return [{'id':'skill_resource:'+skill['id'],'name':skill.get('name','')+' 引用资源',
             'description':'只读已冻结的技能文本附件，不执行脚本', 'kind':'skill_resource','enabled':True,
             'binding':'agent_preparation','stages':['preparation'],'purpose':'style_reference',
             'dependencies':[],'effects':['local_read'],'revision':skill.get('revision',0),
             'version':skill.get('version_hash',''),'input_schema':RESOURCE_SCHEMA,
             'resource_names':list(skill.get('resources',{}))[:50]}
            for skill in skills[:3] if skill.get('id') and skill.get('resources')]


def read_resource(skill, arguments):
    try:
        Draft202012Validator(RESOURCE_SCHEMA).validate(arguments)
        path=arguments['path']
        parsed=PurePosixPath(path.replace('\\','/'))
        if parsed.is_absolute() or '..' in parsed.parts or ':' in path or path not in skill.get('resources',{}):
            raise ValueError('resource not selected')
        content=skill['resources'][path]
        if not isinstance(content,str):
            raise ValueError('resource is not text')
    except Exception as exc:
        raise CapabilityError('SKILL_RESOURCE_INVALID','引用资源路径或参数无效') from exc
    offset,limit=arguments.get('offset',0),arguments.get('limit',12000)
    fragment=content[offset:offset+limit]
    return {'skill_id':skill['id'],'version':skill.get('version_hash',''),'path':path,
            'content':fragment,'offset':offset,'total_characters':len(content),
            'next_offset':offset+len(fragment) if offset+len(fragment)<len(content) else None,
            'loaded_resources':1,'loaded_characters':len(fragment)}
