"""Choose project inference settings without collecting or copying secrets."""
from astra_core.llm.project_config import MODEL_FIELDS, validate_profile
from astra_designer.llm.config import preferences


def designer_profile(config_path, client=None):
    values = ({key: getattr(client, key) for key in MODEL_FIELDS} if client is not None
              else preferences(config_path))
    from astra_core.llm.config import credential_environment
    return validate_profile({**values, 'api_key_env': credential_environment(config_path)})


def choose_project_model(config_path, client=None):
    while True:
        choice = input('项目模型：1 留白，稍后配置；2 复制设计器当前模型配置 [1]: ').strip()
        if choice in {'', '1'}:
            print('项目模型留白，生成后填写 configs/model.json；密钥通过环境变量提供。')
            return validate_profile({})
        if choice == '2':
            profile = designer_profile(config_path, client)
            print(f"将复制模型地址、名称和参数；不复制密钥。运行时通过 {profile['api_key_env']} 提供密钥，"
                  '也可修改 configs/model.json 中的 api_key_env。')
            return profile
        print('请输入 1 或 2。')
