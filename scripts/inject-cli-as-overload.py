import re

from cli.export import api_to_dict


def _cli_to_schema(
    api_dict,
    target,
):
    pod_api = None

    for d in api_dict['methods']:
        if d['name'] == target:
            pod_api = d['options']
            break

    _schema = {
        'properties': {},
        'required': [],
    }

    for p in pod_api:
        dtype = p['type']
        if dtype.startswith('typing.'):
            dtype = dtype.replace('typing.', '')
        pv = {'description': p['help'].strip(), 'type': dtype, 'default': p['default']}
        if p['choices']:
            pv['enum'] = p['choices']
        if p['required']:
            _schema['required'].append(p['name'])
        if dtype == 'array':
            _schema['items'] = {'type': 'string', 'minItems': 1, 'uniqueItems': True}

        pv['default_literal'] = pv['default']
        if isinstance(pv['default'], str):
            pv['default_literal'] = "'" + pv['default'] + "'"
        if p['default_random']:
            pv['default_literal'] = None

        # special cases
        if p['name'] == 'log_config':
            pv['default_literal'] = None

        pv['description'] = pv['description'].replace('\n', '\n' + ' ' * 10)

        _schema['properties'][p['name']] = pv

    return sorted(_schema['properties'].items(), key=lambda k: k[0])


# param
entries = [
    dict(
        cli_entrypoint='pod',
        doc_str_title='Add an Executor to the current Flow object.',
        doc_str_return='a (new) Flow object with modification',
        return_type='BaseFlow',
        filepath='../jina/flow/base.py',
        overload_fn='add',
    ),
    dict(
        cli_entrypoint='flow',
        doc_str_title='Create a Flow. Flow is how Jina streamlines and scales Executors',
        doc_str_return='the new Flow object',
        return_type=None,
        filepath='../jina/flow/base.py',
        overload_fn='__init__',
    ),
]


def fill_overload(
    cli_entrypoint,
    doc_str_title,
    doc_str_return,
    return_type,
    filepath,
    overload_fn,
    indent=' ' * 4,
):
    a = _cli_to_schema(api_to_dict(), cli_entrypoint)
    cli_args = [
        f'{indent}{indent}{k[0]}: Optional[{k[1]["type"]}] = {k[1]["default_literal"]}'
        for k in a
    ]
    args_str = ', \n'.join(cli_args + [f'{indent}{indent}**kwargs'])
    signature_str = f'def {overload_fn}(\n{indent}{indent}self,\n{args_str})'
    if return_type:
        signature_str += f' -> \'{return_type}\':'
        return_str = f'\n{indent}{indent}:return: {doc_str_return}'
    else:
        signature_str += ':'
        return_str = ''
    doc_str = '\n'.join(
        f'{indent}{indent}:param {k[0]}: {k[1]["description"]}' for k in a
    )
    noqa_str = '\n'.join(
        f'{indent}{indent}.. # noqa: DAR{j}' for j in ['202', '101', '003']
    )

    final_str = f'@overload\n{indent}{signature_str}\n{indent}{indent}"""{doc_str_title}\n\n{doc_str}{return_str}\n\n{noqa_str}\n{indent}{indent}"""'

    final_code = re.sub(
        rf'(# overload_inject_start_{cli_entrypoint}).*(# overload_inject_end_{cli_entrypoint})',
        f'\\1\n{indent}{final_str}\n{indent}\\2',
        open(filepath).read(),
        0,
        re.DOTALL,
    )

    with open(filepath, 'w') as fp:
        fp.write(final_code)


if __name__ == '__main__':
    for d in entries:
        fill_overload(**d)
